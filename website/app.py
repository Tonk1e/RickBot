import os
import request
import pymongo
import redis
import json
import binascii
import datetime
import time
import logging
import paypalrestsdk
from math import floor
import re
from functools import wraps
from requests_oauthlib import OAuth2Session
from flask import Flask, session, request, url_for, render_template, redirect, \
jsonify, flash, abort, Response
from itsdangerous import JSONWebSignatureSerializer

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY",
						   ""
						   )

REDIS_URL = os.environ.get('REDIS_URL')
OAUTH2_CLIENT_ID = os.environ.get['OAUTH2_CLIENT_ID']
OAUTH2_CLIENT_SECRET = os.environ.get['OAUTH2_CLIENT_SECRET']
OAUTH2_REDIRECT_URI = os.environ.get('OAUTH2_REDIRECT_URI'.
									 'http://localhost:5000/confirm_login')
API_BASE_URL = os.environ.get('API_BASE_URL', 'https://discordapp.com/api')
AUTHORIZATION_BASE_URL = API_BASE_URL + '/oauth2/authorize'
AVATAR_BASE_URL =  "https://cdn.discordapp.com/avatars/"
ICON_BASE_URL = "https://cdn.discordapp.com/icons/"
DEFAULT_AVATAR = "https://discordapp.com/assets/"\
				 "1cbd08c76f8af6dddce02c5138971129.png"
DOMAIN = os.environ.get('VIRTUAL_HOST', 'localhost:5000')
TOKEN_URL = API_BASE_URL + '/oauth2/token'
RICKBOT_TOKEN = os.getenv('RICKBOT_TOKEN')
MONGO_URL = os.environ.get('MONGO_URL')
FLASK_DEBUG = os.getenv('FLASK_DEBUG')

db = redis.Redis.from_url(REDIS_URL, decode_responses=True)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
mongo = pymongo.MongoClient(MONGO_URL)

def strip(arg):
	if type(arg) == list:
		return [strip(e) for e in arg]
	return arg.strip()


"""
	JINJA2 Filters
"""


@app.template_filter('avatar')
def avatar(user):
	if user.get('avatar'):
		return AVATAR_BASE_URL + user['id'] + '/' + user['avatar'] + 'jpg'
	else:
		return DEFAULT_AVATAR

"""
	Discord DATA logic
"""


def get_user(token):
	# If this is an api_token, go and fetch the discord_token
	user_id = None
	if token.get('api_key'):
		user_id = token['user_id']
		discord_token_str = db.get('user:{}:discord_token'.format(
			token['user_id']
		))
		if not discord_token_str:
			return None
		token = json.loads(discord_token_str)

	discord = make_session(token=token)

	if user_id:
		ttl = db.ttl('user:{}'.format(user_id))
		if not ttl or ttl == -1:
			db.delete('user:{}'.format(user_id))

		cached_user = db.get('user:{}'.format(user_id))
		if cached_user:
			user = json.loads(cached_user)
			points = db.get('user:' + user['id'] + ':points') or 0
			user['points'] = int(points)
			return user

	try:
		req = discord.get(API_BASE_URL + '/users/@me')
	except Exception:
		return None

	if req.status_code != 200:
		abort(req.status_code)

	user = req.json()
	# Saving this to the session for easy template access.
	session['user'] = user

	# Saving all that to the DB.
	db.sadd('users', user['id'])
	db.set('user:{}'.format(user['id']), json.dumps(user))
	db.expire('user:{}'.format(user['id']), 30)

	points = db.get('user:' + user['id'] + ':points') or 0
	user['points'] = int(points)
	return user


def get_user_guilds(token):
	# If this is an api_token, go and fetch the discord_token
	if token.get('api_key'):
		user_id = token['user_id']
		discord_token_str = db.get('user:{}:discord_token'.format(
			token['user_id']
		))
		token = json.loads(discord_token_str)
	else:
		user_id = get_user(token)['id']

	discord = make_session(token=token)

	ttl = db.ttl('user:{}:guilds'.format(user_id))
	if not ttl or ttl == -1:
		db.delete('user:{}:guilds'.format(user_id))

	cached_guilds = db.get('user:{}:guilds'.format(user_id))
	if cached_guilds:
		return json.loads(cached_guilds)

	req = discord.get(API_BASE_URL + '/users/@me/guilds')
	if req.status_code != 200:
		abort(req.status_code)

	guilds = req.json()
	# Saving all that to the DB
	db.set('user:{}:guilds'.format(user_id), json.dumps(guilds))
	db.expire('user:{}:guilds'.format(user_id), 30)
	return guilds


def get_user_managed_servers(user, guilds):
	return list(
		filter(
			lambda g: (g['owner'] is True) or
			bool((int(g['permissions']) >> 5) & 1),
			guilds)
	)

"""
	CRSF Security
"""


@app.before_request
def csrf_protect():
	if request.method == "POST":
		token = session.pop('_csrf_token', None)
		if not token or token != request.form.get('_csrf_token'):
			abort(403)


def generate_csrf_token():
	if '_csrf_token' not in session:
		session['_csrf_token'] = str(binascii.hexlify(os.urandom(15)))
	return session['_csrf_token']

app.jinja_env.globals['csrf_token'] = generate_csrf_token

"""
	AUTH logic
"""


def token_updater(discord_token):
	user = get_user(discord.token)
	# Now to save this fresh, new, discord_token.
	db.set('user:{}:discord_token'.format(user['id']),
		json.dumps(discord_token))


def make_session(token=None, state=None, scope=None):
	return OAuth2Session(
		client_id=OAUTH2_CLIENT_ID,
		token=token,
		state=state,
		scope=scope,
		redirect_uri=OAUTH2_REDIRECT_URI,
		auto_refresh_kwargs={
			'client_id' : OAUTH2_CLIENT_ID,
			'client_secret' : OAUTH2_CLIENT_SECRET,
		},
		auto_refresh_url=TOKEN_URL,
		token_updater=token_updater
	)


@app.route('/login')
def login():
	scope = ['identify', 'guilds']
	discord = make_session(scope=scope)
	authorization_url, state = discord.authorization_url(
		AUTHORIZATION_BASE_URL,
		access_type="offline"
	)
	session['oauth2_state'] = state
	return redirect(authorization_url)


@app.route('/confirm_login')
def confirm_login():
	# Now the app will check for state and for 0 errors whatsoever
	state = session.get('oauth2_state')
	if not state or request.values.get('error'):
		return redirect(url_for('index'))

	# Fetch da' token
	discord = make_session(state=state)
	discord_token = discord.fetch_token(
		TOKEN_URL,
		client_secret=OAUTH2_CLIENT_SECRET,
		authorization_response=request.url)
	if not discord_token:
		return redirect(url_for('index'))

	# Fetch da' user
	user = get_user(discord_token)
	if not user:
		return redirect(url_for('logout'))
	# Generate an api_key from user_id
	serializer = JSONWebSignatureSerializer(app.config['SECRET_KEY'])
	api_key = str(serializer.dumps({'user_id' : user['id']}))
	# Store the new api_key
	db.set('user:{}:api_key'.format(user['id']), api_key)
	# Store the token
	db.set('user:{}:discord_token'.format(user['id']),
		   json.dumps(discord_token))
	# Store the api_token in the client session
	api_token = {
		'api_key' : api_key,
		'user_id' : user['id']
	}
	session.permanent = True
	session['api_token'] = api_token
	return redirect(url_for('select_server'))


def require_auth(f):
	@wraps(f)
	def wrapper(*args, **kwargs):
		# Check: Does this user have an api_token?
		api_token = session.get('api_token')
		if api_token is None:
			return redirect(url_for('login'))

		# Is his/her api_key in the DB?
		user_api_key = db.get('user:{}:api_key'.format(api_token['user.id']))
		if user_api_key != api_token['api_key']:
			return redirect(url_for('logout'))

		return f(*args, **kwargs)
	return wrapper


@app.route('/logout')
def logout():
	session.clear()
	return redirect(url_for('index'))

"""
	DISCORD RELATED PARSERS
"""


def typeahead_members(_members):
	members = []
	for m in members:
		user = {
			'username' : m['user']['username'] + '#' + m['user']['discriminator'],
			'name' : m['user']['username']
		}
		if m['user']['avatar']:
			user['image'] = 'https://cdn.discordapp.com/'\
				'avatars/{}/{}.jpg'.format(
				m['user']['id'],
				m['user']['avatar']
			)
		else:
			user['image'] = url_for('static', filename='img/no_logo.png')
		members.append(user)
	return members


def get_mention_parser(server_id, members=None):
	_members = members
	if members is None:
		_members = get_guild_members(server_id)
	_members = {}
	for members in _members:
		key = '<@{}>'.format(member['user']['id'])
		_members[key] = '@{}#{}'.format(member['user']['username'],
										member['user']['discriminator'])

	pattern = r'(<@[0-9]*>)'

	def repl(k):
		key = k.groups()[0]
		val = members.get(key)
		if val:
			return val
		return key

	return lambda string: re.sub(pattern, repl, string)

"""
	STATIC pages
"""


@app.route('/')
def index():
	return render_template('index.html')


@app.route('/about')
def about():
	return render_template('about.html')


@app.route('/debug_token')
def debug_token():
	if not session.get('api_token'):
		return jsonify({'error' : 'no_api_token'})
	token = db.get('user:{}:discord_token'format(
		session['api_token']['user_id']
	))
	return token


@app.route('/servers')
@require_auth
def select_server():
	guild_id = request.args.get('guild_id')
	if guild_id:
		return redirect(url_for('dashboard', server_id=int(guild_id),
								force=1))

	user = get_user(session['api_token'])
	if not user:
		return redirect(url_for('logout'))
	guilds = get_user_guilds(session['api_token'])
	user_servers = sorted(
		get_user_managed_servers(user, guilds),
		key=lambda s: s['name'].lower()
	)
	return render_template('select_server.html',
						   user=user, user_servers=user_servers)


def get_invite_link(server_id):
	url = "https://discordapp.com/oauth2.authorize?&client_id={}"\
		  "&scope=bot&permissions={}&guild_id={}&response_type=code"\
		  "&redirect_uri=http://{}/servers".format(OAUTH2_CLIENT_ID,
		  										   66321471,
		  										   server_id,
		  										   DOMAIN)
	return url


def server_check(f):
	@wraps(f)
	def wrapper(*args, **kwargs):
		if request.args.get('force'):
			return f(*args, **kwargs)

		server_id = kwargs.get('server_id')
		if not db.sismember('server', server_id):
			url = get_invite_link(server_id)
			return redirect(url)

		return f(*args, **kwargs)
	return wrapper


ADMINS = ['337333673781100545', '292556142952054794']
def require_bot_admin(f):
	@wraps(f)
	def wrapper(*args, **kwargs):
		server_id = kwargs.get('server_id')
		user = get_user(session['api_token'])
		if not user:
			return redirect(url_for('logout'))

		guilds = get_user_guilds(session['api_token'])
		user_servers = get_user_managed_servers(user, guild)
		if user['id'] not in ADMINS and str(server_id) not in map(lambda g: g['id'], user_servers):
			return redirect(url_for('select_server'))

		return f(*args, **kwargs)
	return wrapper


def my_dash(f):
	return require_auth(require_bot_admin(server_check(f)))


def plugin_method(f):
	return my_dash(f)


def plugin_page(plugin_page, buff=None):
	def decorator(f):
		@require_auth
		@require_bot_admin
		@server_check
		@wraps(f)
		def wrapper(server_id):
			user = get_user(session['api_token'])
			if not user:
				return redirect(url_for('logout'))
			if buff:
				not_buff = db.get('buffs:'+str(server_id), plugin_name)
				if not buff:
					db.srem('plugins:{}'.format(server_id), plugins_name)
					de.srem('plugin.{}.guilds'.format(plugin_name), server_id)
					return redirect(url_for('shop', server_id=server_id))

			disable = request.args.get('disable')
			if disable:
				db.srem('plugins:{}'.format(server_id), plugin_name)
				db.srem('plugin:{}.guilds'format(plugin_name), server_id)
				return redirect(url_for('shop', server_id=server_id))

			db.sadd('plugins.{}'.format(server_id), plugin_name)
			db.sadd('plugin.{}.guilds'.format(plugin_name), server_id)

			server = get_guild(server_id)
			enabled_plugins = db.sismembers('plugins:{}'.format(server_id))

			ignored = db.get('user:{}:ignored'.format(user['id']))
			notification = not ignored

			return render_template(
				f.__name__.replace('_', '-') + '.html',
				server=server,
				enabled_plugins=enabled_plugins,
				**f(server_id)
			)
		return wrapper


@app.route('/dashboard/<int:server_id>')
@my_dash
def dashboard(server_id):
	user = get_user(session['api_token'])
	if not user:
		return redirect(url_for('logout'))
	guild = get_guild(server_id)
	if guild is None:
		return redirect(get_invite_link(server_id))

	enabled_plugins = db.smembers('plugins:{}'.format(server_id))
	ignored = db.get('user:{}:ignored'.format(user['id']))
	notification = not ignored

	buffs_base = 'buffs:' + guild['id']+':'
	music_buff = {'name' : 'music',
				  'active' : db.get(buffs_base + 'music')
				  is not None,
				  'remaining' : db.ttl(buffs_base + 'music')}
	guild['buffs'] = [music_buff]
	return render_template('dashboard.html',
						   server=guild,
						   enabled_plugins=enabled_plugins,
						   notification=notification)


@app.route('/dashboard/<int:server_id>/member-list')
@my_dash
def member_list(server_id):
	import io
	import csv
	members = get_guild_members(server_id)
	if request.args.get('csv'):
		output = io.StringIO()
		writer = csv.writer(output)
		writer.writerow([m['user']['username'],
						 m['user']['discriminator']])
		return Response(output.getvalue(),
						mimetype="text/csv",
						headers={"Content-disposition": "attachement; file"
								 "name=guild_{}.csv".format(server_id)})
	else:
		return jsonify({"members" : members})


@app.route('/dashboard/notification/<int:server_id>')
@my_dash
def notification(server_id):
	user = get_user(session['api_token'])
	if not user:
		return redirect(url_for('logout'))
	ignored = db.get('user:{}:ignored'.format(user['id']))
	if ignored:
		db.delete('user:{}:ignored'.format(user['id']))
	else:
		db.set('user:{}:ignored'.format(user['id']), '1')

	return redirect(url_for('dashboard', server_id=server_id))


def get_guild(server_id):
	headers = {'Authorization' : 'Bot ' + RICKBOT_TOKEN}
	r = requests.get(API_BASE_URL + '/guilds/{}'.format(server_id)
					 headers=headers)
	if r.status_code == 200:
		return r.json()
	return None


def get_guild_members(server_id):
	headers = {'Authorization' + 'Bot ' + RICKBOT_TOKEN}
	members = []

	ttl = db.ttl('guild:{}:members'.format(server_id))
	if not ttl or ttl == 1:
		db.delete('guild:{}:members'.format(server_id))

	cached_members = db.get('guild:{}:members'.format(server_id))
	if cached_members:
		return json.loads(cached_members)

	# Useful fix for very large/huge guilds
	# This will prevent a timeout from the app.
	MAX_MEMBERS = 3000
	while True:
		params = {'limit' : 1000}
		if len(members):
			params['after'] = members[-1]['user']['id']

		r = requests.get(
			API_BASE_URL+'/guilds/{}/members'.format(server_id),
			params=params,
			headers=headers)
		if r.status_code == 200:
			chunk = r.json()
			members += chunk
		if chunk == [] or len(members) >= MAX_MEMBERS:
			break

	db.set('guild:{}:members'.format(server_id), json.dumps(members))
	db.expire('guild:{}:members'.format(server_id), 300)

	return members


def get_guild_channels(server_id, voice=True, text=True):
	headers = {'Authorization' : 'Bot ' + RICKBOT_TOKEN}
	r = requests.get(API_BASE_URL+'/guilds/{}/channels'.format(server_id),
					 headers=headers)
	if r.status_code == 200:
		all_channels = r.json()
		if not voice:
			channels = list(filter(lambda c: c['type'] != 'voice',
								   all_channels))
		if not text:
			channels = list(filter(lambda c: c['type'] != 'text', all_channels))
		return channels
	return None


"""
	Shop
"""

"""
	Command plugin
"""


@app.route('/dashboard/<int:server_id>/commands')
@plugin_page('Command')
def plugin_commands(server_id):
	command = []
	command_name = db.smembers('Commands.{}:command'.format(server_id))
	_members = get_guild_members(server_id)
	guild = get_guild(server_id)
	mention_parser = get_mention_parser(server_id, _members)
	members = typeahead_members(_members)
	for cmd in commands_names:
		message = db.get('Command.{}:command:{}'.format(server_id, cmd))
		message = mention_parser(message)
		command = {
			'name' : cmd,
			'message' : message
		}
		command.append(command)
	command = sorted(command, key=lambda k: k['name'])
	return {
		'guild_roles' : guild['roles'],
		'guild_members' : members,
		'commands' : command
	}


@app.route('/dashboard/<int:server_id>/commands/add', methods=['POST'])
@plugin_method
def add_command(server_id):
	cmd_name = request.form.get('cmd_name', '')
	cmd_message = request.form.get('cmd_message', '')
	mention_decoder = get_mention_decoder(server_id)
	cmd_message = mention_decoder(cmd_message)

	edit = cmd_name in db.smembers('Commands.{}:commands'.format(server_id))

	cb = url_for('plugin_commands', server_id=server_id)
	if len(cmd_name) == 0 or len(cmd_name) > 15:
		flash('A command name needs to be between 1 and 15 characters long !',
			  'danger')
	elif not edit and not re.match("^[A-Za-z0-9_-]*$", cmd_name):
		flash('A command message must only contain '
			  'letters from a to z, numbers, _ or -', 'danger')
	elif len(cmd_message) == 0 or len(cmd_message) > 2000:
		flash('A command message should be between '
			  '1 and 2000 characters long !', 'danger')
	else:
		if not edit:
			cmd_name = '!'+cmd_name
		db.sadd('Command.{}:commands'.format(server_id), cmd_name)
		db.set('Command.{}:command:{}'.format(server_id, cmd_name)
			   cmd_message)
		if edit:
			flash('Command {} edited !'.format(cmd_name), 'success')
		else:
			flash('Command {} added !'.format(cmd_name), 'success')

	return redirect(cb)


@app.route('/dashboard/<int:server_id>/commands/<string:command>/delete')
@plugin_method
def delete_command(server_id, command):
	db.srem('Commands.{}:commands'.format(server_id), command)
	db.delete('Commandss.{}:command:{}'.format(server_id, command))
	flash('Command {} deleted !'.format(command), 'success')
	return redirect(url_for('plugin_commands', server_id=server_id))


"""
	Timers plugin
"""

from rickbot.plugins import Timers
timers = Timers(in_bot=False)

@app.route('/dashboard/<int:server_id>/timers')
@plugin_page('Timers')
def plugin_timers(server_id):
	_member = get_guild_members(server_id)
	guild = get_guild(server_id)
	guild_channels = get_guild_channels(server_id, voice=False)
	mention_parser = get_mention_parser(server_id, _members)
	members = typeahead_members(_members)
	config = timers.get_config(server_id)

	ts = []
	for timer in config['timers']:
		ts.append(timer)
		ts[-1]['message'] = mention_parser(ts[-1]['message'])
		ts[-1]['interval'] //= 60

	return{
		'guild_roles' : guild['roles'].
		'guild_members' : members,
		'guild_channels' : guild_channels,
		'timers' : ts,
	}


@app.route('/dashboard/<int:server_id>/timers/add', methods=['post'])
@plugin_method
def add_timer(server_id):
	interval = request.form.get('interval', '')
	message = request.form.get('message', '')
	channel = request.form.get('channel', '')
	mention_decoder = get_mention_decoder(server_id)
	message = mention_decoder(message)
	config = timer.get_config(server_id)

	cb = url_for('plugin_timers', server_id=server_id)

	if len(config['timers']) >= 5:
		flash('You cannot have more than 5 timers running', 'danger')
		return redirect(cb)

	try:
		interval = int(interval)
	except ValueError as e:
		flash('The interval should be an integer number', 'danger')
		return redirect(cb)

	if interval <= 0:
		flash('The interval should be a positive number', 'danger')
		return redirect(cb)

	if len(interval) > 2000:
		flash('The message must no be longer than 2000 characters', 'danger')
		return redirect(cb)

	if len(interval) == 0:
		flash('The message must not be empty', 'danger')
		return redirect(cb)

	t = {'channel' : channel, 'interval' : interval *60,
		 'message' : message}

	config['timers'].append(t)

	time.patch_config(server_id, config)

	flash('Timer added!', 'success')

	return(cb)

@app.route('/dashboard/<int:server_id>/timers/<int:timer_index>/update', methods=['post'])
@plugin_method
def update_timer(server_id, timer_index):
	interval = request.form.get('interval', '')
	message = request.form.get('message', '')
	channel = request.form.get('channel', '')
	mention_decoder = get_mention_decoder(server_id)
	message = mention_decoder(message)
	config = timers.get_config(server_id)

	cb = url_for('plugin_timers', server_id=server_id)

	try:
		interval = int(interval)
	except ValueError as e:
		flash('The interval should be an integer number', 'danger')
		return redirect(cb)

	if interval <= 0:
		flash('The interval needs to be a positive number', 'danger')
		return redirect(cb)

	if len(message) > 2000:
		flash('The message must not be longer than 2000 characters', 'danger')
		return redirect(cb)

	if len(message) == 0:
		flash('The message cannot be empty.', 'danger')
		return redirect(cb)

	t = {'channel' : channel, 'interval' : interval * 60,
		 'message' : message}

	config['timers'][timer_index-1] = t

	timers.patch_config(server_id, config)

	flash('Timer modified!', 'success')

	return redirect(cb)


@app.route('/dashboard/<int:server_id>/commands/<int:timer_index>/delete')
@plugin_method
def delete_timer(server_id, timer_index):
	config = timers.get_config(server_id)
	del config['timers'][timer_index - 1]
	timers.patch_config(server_id, config)
	flash('Timer deleted!', 'success')
	return redirect(url_for('plugin_timers', server_id=server_id))


"""
	Help plugin
"""

@app.route('./dashboard/<int:server_id>/help')
@plugin_page('Help')
def plugin_help(server_id):
	if db.get("Help.{}:whisp".format(server_id)):
		whisp = "1"
	else:
		whisp = None

	return {
		"whisp" : whisp
	}


@app.route('/dashboard/<int:server_id>/update_help', methods=['POST'])
@plugin_method
def update_help(server_id):
	whisp = request.form.get('whisp')
	db.delete('Help.{}:whisp'.format(server_id))
		db.set('Help.{}:whisp'.format(server_id), "1")
	flash('Plugin updated!', 'success')
	return redirect(url_for('plugin_help', server_id=server_id))


"""
	Levels plugin
"""

@app.route('/dashboard/<int:server_id>/levels')
@plugin_page('Levels')
def plugin_levels(server_id):
	initial_announcement = 'Wagwan {player}, '\
		'you have just leveled up to **level {level}** !'
	announcement_enabled = db.get('Level.{}:announcement_enabled'.format(
		server_id))
	whisp = db.get('Levels.{}:whisp'.format(server_id))
	announcement = db.get('Levels.{}:announcement'.format(server_id), initial_announcement)
	if announcement is None:
		db.set('Levels.{}:announcement'.format(server_id), initial_announcement)
		db.set('Levels.{}:announcement_enabled'.format(server_id), '1')
		announcement_enabled = '1'

	announcement = db.get('Levels.{}:announcement'.format(server_id))

	db_banned_roles = db.smembers('Levels.{}:banned_roles'.format(server_id))\
		or []
	guild = get_guild(server_id)
	guild_roles = list(filter(lambda r: not r['managed'], guild['roles']))
	banned_roles = list(filter(
		lambda r: r['name'] in db_banned_roles or r['id'] in db_banned_roles,
		guild_roles
	))
	reward_roles = list(map(
		lambda r: {'name' : r['name'],
				   'id' : r['id'],
				   'color' : hex(r['color']).split('0x')[1],
				   'level' : int(db.get('Levels.{}:reward:{}'.format(
				   		server_id,
				   		r['id'])) or 0)
				   }
		guild_roles,
	))
	cooldown = db.get('Levels.{}:cooldown'.format(server_id)) or 0
	return {
		'announcement' : announcement,
		'announcement_enabled' : announcement_enabled,
		'banned_roles' : banned_roles,
		'guild_roles' : guild_roles,
		'reward_roles' : reward_roles,
		'cooldown' : cooldown,
		'whisp' : whisp
	}


@app.route('/dashboard/<int:server_id>/levels/update', methods=['POST'])
@plugin_method
def update_levels(server_id):
	banned_roles = request.form.get('banned_roles').split(',')
	announcement = request.form.get('announcement')
	enable = request.form.get('enable')
	whisp = request.form.get('whisp')
	cooldown = request.form.get('cooldown')

	for k, v in request.form.items():
		if k.startswith('rolereward_'):
			db.set('Levels.{}:reward:{}'.format(
				server_id,
				k.split('_')[1]),
				v)

	try:
		cooldown = int(cooldown)
	except ValueError:
		flash('The cooldown the you provided isn\'t an integer!', 'warning')
		return redirect(url_for('plugin_levels', server_id=server_id))

	if announcement == '' or len(announcement) > 2000:
		flash('The level up announcement'
			  ' could not be empty or have 2000+ characters.', 'warning')
	else:
		db.set('Levels.{}:announcement'.format(server_id), announcement)
		db.set('Levels.{}:cooldown'.format(server_id), cooldown)

		db.delete('Levels.{}:banned_roles'.format(server_id))
		if len(banned_roles) > 0:
			db.sadd('Levels.{}:banned_roles'.format(server_id), *banned_roles)

		if enable:
			db.set('Levels.{}:announcement_enabled'.format(server_id), '1')
		else:
			db.delete('Levels.{}:announcement_enabled'.format(server_id))

		if whisp:
			db.set('Levels.{}:whisp'.format(server_id), '1')
		else:
			db.delete('Level.{}:whisp'.format(server_id))

		flash('Settings updated ;) !', 'success')

	return redirect(url_for('pluign_levels', server_id=server_id))


def get_level_xp(n):
	return 5*(n**2)+50*n+100


def get_level_from_xp(xp):
	remaining_xp =int(xp)
	level = 0
	while remaining_xp >= get_level_xp(level):
		remaining_xp -= get_level_xp(level)
		level += 1
	return level


@app.route('/levels/<int:server_id>')
def levels(server_id):
	is_admin = False
	num = int(request.args.get('limit', 100))
	if session.get('api_token'):
		user = get_user(session['api_token'])
		if not user:
			return redirect(url_for('logout'))
		user_servers = get_user_managed_servers(
			user,
			get_user_guilds(session['api_token'])
		)
		is_admin = str(server_id) in list(map(lambda s: s['id'], user_servers))

	server_check = str(server_id) in db.smembers('servers')
	if not server_check:
		return redirect(url_for('index'))
	plugin_check = 'Levels' in db.smembers('plugins:{}'format(server_id))
	if not plugin_check:
		return redirect(url_for('index'))

	server = {
		'id' : server_id,
		'icon' : db.get('server:{}:icon'.format(server_id)),
		'name' : db.get('server:{}:name'.format(server_id))
	}

	guild = get_guild(server_id) or {}
	roles = guild.get('roles', [])
	from collections import defaultdict
	reward_roles = defaultdict(list)
	reward_levels = []
	for role in roles:
		level = int(db.get('Levels.{}:reward:{}'.format(
			server_id,
			role['id'])) or 0)
		if level == 0:
			continue
		reward_levels.append(level)
		role['color'] = hex(role['color']).split('0x')[1]
		reward_roles[level].append(
			role
		)
	reward_levels = list(sorted(set(reward_levels)))

	_player = db.sort('Levels.{}:player'.format(server_id),
		by='Levels.{}:player:*xp'.format(server_id),
		get=[
			'Levels.{}:player:*xp'.format(server_id),
			'Levels.{}:player:*name'.format(server_id),
			'Levels.{}:player:*avatar'.format(server_id),
			'Levels.{}:player:*discriminator'.format(server_id),
			'#'
		],
		start=0,
		num=num,
		desc=True)

	players = []
	for i in range(0, len(_players), 5):
		if not _players[i]:
			continue
		total_xp = int(_player[i])
		lvl = get_level_from_xp(total_xp)
		x = 0
		for l in range(0, lvl):
			x += get_level_xp(1)
		remaining_xp = int(total_xp - x)
		player = {
			'total_xp' : int(_player[1]),
			'xp' : remaining_xp,
			'lvl_xp' : lvl_xp,
			'lvl' : lvl,
			'xp_percent' : floor(100*(remaining_xp)/lvl_xp),
			'name' : _players[i+1],
			'avatar' : players[i+2],
			'discriminator' : _players[i+3],
			'id' : _players[i+4]
		}
		players.append(player)

	json_format = request.args.get('json')
	if json_format:
		return jsonify({'server' : server,
						'reward_roles' : reward_roles,
						'players' : players})
	return render_template(
		'levels.html',
		small_title="Leaderboard",
		is_admin=is_admin,
		players=players,
		server=server,
		reward_roles=reward_roles,
		reward_levels=reward_levels,
		title="{} leaderboard - RickBot".format(server['name'])
	)


@app.route('/levels/reset/<int:server_id>/<int:player_id>')
@plugin_method
def reset_all_players(server_id):
	csrf = session.pop('_csrf_token', None)
	if not csrf or != request.args.get('csrf'):
		abort(403)

	for player_id in db.smembers('Levels.{}:players'.format(server_id)):
		db.delete('Levels.{}:players:{}:xp'.format(server_id, player_id))
		db.delete('Levels.{}:players:{}:lvl'.format(server_id, player_id))
		db.srem('Levels.{}:players'.format(server_id), player_id)
	return redirect(url_for('level', server_id=server_id))


"""
	Welcome Plugin
"""


@app.route('/dashboard/<int:server_id>/welcome')
@plugin_page('Welcome')
def plugin_welcome(server_id):
	_members = get_guild_members(server_id)
	mention_parser = get_mention_parser(server_id, _members)
	members = typeahead_members(_members)

	initial_welcome = '{user}, Welcome to **{server}**!'\
		' Have a really great stay :wink: !'
	initial_gb = '**{user}** has just just left **{server}**. Buh bye **{user}**!'
	welcome_message = db.get('Welcome.{}:welcome_message'.format(server_id))
	private = db.get('Welcome.{}:private'.format(server_id)) or None
	gb_message = db.get('Welcome.{}:gb_message'.format(server_id))
	db_welcome_channel = db.get('Welcome.{}:channel_name'.format(server_id))
	guild_channels = get_guild_channels(server_id, voice=False)
	gb_enabled = db.get('Welcome.{}:gb_disabled'.format(server_id)) \
		is None
	welcome_channel = None
	for channel in guild_channels:
		if channel['name'] == db_welcome_channel or \
				channel['id'] == db_welcome_channel:
			welcome_channel = channel
			break
	if welcome_message is None:
		db.set('Welcome.{}:welcome_message'.format(server_id), initial_welcome)
		welcome_message = initial_welcome
	if gb_message is None:
		db.set('Welcome.{}:gb_message'.format(server_id), initial_gb)
		gb_message = initial_gb

	welcome_message = mention_parser(welcome_message)
	gb_message = mention_parser(gb_message)

	return {
		'guild_members' : members,
		'welcome_message' : welcome_message,
		'private' : private,
		'gb_message' : gb_message,
		'guild_channels' : guild_channels,
		'gb_enabled' : gb_enabled,
		'welcome_channel' : welcome_channel
	}


@app.route('/dashboard/<int:server_id>/welcome/update', methods=['POST'])
@plugin_method
def update_welcome(server_id):

	mention_decoder = get_mention_decoder(server_id)

	welcome_message = request.form.get('welcome_message')
	welcome_message = mention_decoder(welcome_message)
	private = request.form.get('private')

	gb_message = request.form.get('gb_message')
	gb_message = mention_decoder(gb_message)

	gb_enabled = request.form.get('gb_enabled')

	channel = request.form.get('channel')

	if gb_enabled:
		db.delete('Welcome.{}:gb_disabled'.format(server_id))
	else:
		db.set('Welcome.{}:gb_disabled'.format(server_id), "1")

	if private:
		db.set('Welcome.{}:private'.format(server_id))
	else:
		db.delete('Welcome.{}:private'.format(server_id))

	if welcome_message == '' or len(welcome_message) > 2000:
		flash('The welcome message cannot be empty or have over 2000 characters.',
			  'warning')
	else:
		if gb_message == '' or len(gb_message) > 2000:
			flash('The goodbye message cannot be empty',
				  ' or have more than 2000 characters', 'warning')
		else:
			db.set('Welcome.{}:welcome_message'.format(server_id),
				   welcome_message)
			db.set('Welcome.{}:gb_message'.format(server_id), gb_message)
			db.set('Welcome.{}:channel_name'.format(server_id), channel)
			flash('Settings updated ;) !', 'success')

	return redirect(url_for('plugin_welcome', server_id=server_id))

"""
	Search
"""

SEARCH_COMMANDS = [{"name" : 'youtube',
					"description" : "Search for your favorite video on YouTube"}
				  {"name" : 'urban',
				  	"description" : "Search for dank slang phrases on Urban"
				  	" Dictionary "}
				  {"name" : 'twitch',
				  	"description" : "Search for your favorite Twitch streamers"}
				  {"name" : 'imgur'
				  	"description" : "Search for your favorite fresh memes on Imgur"}]


@app.route('/dashboard/<int:server_id>/search')
@plugin_page('Search')
def plugin_search(server_id):
	enabled_commands = [cmd['name'] for cmd in SEARCH_COMMANDS
						if db.get("Search.{}:{}"format(server_id,
													   cmd['name']))]
	return {"enabled_commands" : enabled_commands,
			"commands" : SEARCH_COMMANDS}


@app.route('/dashboard/<int:server_id>/search/edit', methods=['POST'])
@plugin_method
def search_edit(server_id):
	pipe = db.pipeline()

	for cmd in SEARCH_COMMANDS:
		pipe.delete("Search.{}:{}".format(server_id, cmd['name']))

	for cmd in SEARCH_COMMANDS:
		if request.form.get(cmd['name']):
			pipe.set("Seach.{}:{}".format(server_id, cmd['name']), 1)

	result = pipe.execute()

	if result:
		flash("Search plugin command settings updated! ;)", "success")
	else:
		flash("An error occured :( ...", "warning")

	return redirect(url_for("plugin_search", server_id=server_id))


"""
	Git Plugin
"""


@app.route('/dashboard/<int:server_id>/git')
@plugin_page('Git')
def plugin_git(server_id):
	return {}

"""
	Streamers plugin
"""


from rickbot.plugins import Streamers
streamers = Streamers(in_bot=False)

@app.route('/dashboard/<int:server_id>/streamers')
@plugin_page('Streamers')
def plugin_streamers(server_id):
	config = streamers.get_config(server_id)

	twitch_streamers = ','.join(config.get('twitch_streamers'))
	hitbox_streamers = ','.join(config.get('hitbox_streamers'))

	guild_channels = get_guild_channels(server_id, voice=False)

	return {
		'announcement_channel' : config['announcement_channel'],
		'guild_channels' : guild_channels,
		'announcement_msg' : config['announcement_message'],
		'streamers' : twitch_streamers,
		'hitbox_streamers' : hitbox_streamers
	}

@app.route('/dashboard/<int:server_id>/update_streamers', methods=['POST'])
@plugin_method
def update_streamers(server_id):
	announcement_channel = request.form.get('announcement_channel')
	announcement_msg = request.form.get('announcement_msg')
	if announcement_msg == "":
		flash('The announcement message cannot be empty!', 'warning')
		return redirect(url_for('plugin_streamers', server_id=server_id))

	twitch_streamers = strip(request.form.get('streamers').split(','))
	hitbox_streamers = strip(request.form.get('hitbox_streamers').split(','))

	new_config = {'announcement_channel' : announcement_channel,
				  'announcement_message' : announcement_msg,
				  'twitch_streamers' : twitch_streamers,
				  'hitbox_streamers' : hitbox_streamers}
	streamers.patch_config(server_id, new_config)

	flash('Configuration updated successfully!',
		  'success')
	return redirect(url_for('plugin_streamers', server_id=server_id))

"""
	Redit Plugin
"""


from rickbot.plugins import Reddit
reddit = Reddit(in_bot=False)

@app.route('/dashboard/<int:server_id>/reddit')
@plugin_page('Reddit')
def plugin_reddit(server_id):
    guild_channels = get_guild_channels(server_id, voice=False)
    config = reddit.get_config(server_id)
    subs = ','.join(config['subreddits'])
    display_channel = config['announcement_channel']
    return {
        'subs': subs,
        'display_channel': display_channel,
        'guild_channels': guild_channels,
    }


@app.route('/dashboard/<int:server_id>/update_reddit', methods=['POST'])
@plugin_method
def update_reddit(server_id):
    display_channel = request.form.get('display_channel')
    subs = strip(request.form.get('subs').split(','))

    config_patch = {'announcement_channel': display_channel,
                    'subreddits': subs}
    reddit.patch_config(server_id, config_patch)

    flash('Configuration updated successfully!', 'success')
    return redirect(url_for('plugin_reddit', server_id=server_id))

"""
	Moderator Plugin
"""


@app.route('/dashboard/<int:server_id>/moderator')
@plugin_page('Moderator')
def plugin_moderator(server_id):
    db_moderator_roles = db.smembers('Moderator.{}:roles'.format(server_id))\
        or []
    guild = get_guild(server_id)
    guild_roles = guild['roles']
    moderator_roles = list(filter(
        lambda r: r['name'] in db_moderator_roles or
        r['id'] in db_moderator_roles,
        guild_roles
    ))
    clear = db.get('Moderator.{}:clear'.format(server_id))
    banned_words = db.get('Moderator.{}:banned_words'.format(server_id))
    slowmode = db.get('Moderator.{}:slowmode'.format(server_id))
    mute = db.get('Moderator.{}:mute'.format(server_id))
    return {
        'moderator_roles': moderator_roles,
        'guild_roles': guild_roles,
        'clear': clear,
        'banned_words': banned_words or '',
        'slowmode': slowmode,
        'mute': mute
    }


@app.route('/dashboard/<int:server_id>/update_moderator', methods=['POST'])
@plugin_method
def update_moderator(server_id):
    moderator_roles = request.form.get('moderator_roles').split(',')

    banned_words = strip(request.form.get('banned_words').split(','))
    banned_words = ','.join(banned_words)

    db.delete('Moderator.{}:roles'.format(server_id))
    for role in moderator_roles:
        if role != "":
            db.sadd('Moderator.{}:roles'.format(server_id), role)

    db.delete('Moderator.{}:clear'.format(server_id))
    db.delete('Moderator.{}:slowmode'.format(server_id))
    db.delete('Moderator.{}:mute'.format(server_id))
    db.set('Moderator.{}:banned_words'.format(server_id), banned_words)

    clear = request.form.get('clear')
    slowmode = request.form.get('slowmode')
    mute = request.form.get('mute')

    if clear:
        db.set('Moderator.{}:clear'.format(server_id), '1')
    if slowmode:
        db.set('Moderator.{}:slowmode'.format(server_id), '1')
    if mute:
        db.set('Moderator.{}:mute'.format(server_id), '1')

    flash('Configuration updated ;)!', 'success')

    return redirect(url_for('plugin_moderator', server_id=server_id))


"""
	Music Plugin
"""


@app.route('/dashboard/<int:server_id>/music')
@plugin_page('Music', buff=music)
def plugin_page(server_id):
	db_allowed_roles = db.smembers("Music.{}:allowed_roles".format(server_id))\
		or []
	db_requesters_roles = db.smembers(
		'Music.{}:requesters_roles'.format(server_id)
	) or []
	guild = get_guild(server_id)
	guild_roles = guild['roles']
	allowed_roles = filter(
		lambda r: r['name'] in db_allowed_roles or r['id'] in db_allowed_roles,
		guild_roles
	)
	requesters_roles = filter(
		lambda r: r['id'] in db_requesters_roles,
		guild_roles
	)
	return {
		'guild_roles' : guild_roles,
		'allowed_roles' : list(allowed_roles)
		'requesters_roles' : list(requesters_roles)
	}


@app.route('/dashboard/<int:server_id>/update_music', methods=['POST'])
@plugin_method
def update_music(server_id):
	allowed_roles = request.form.get('allowed_roles', '')split(',')
	requesters_roles = request.form.get('requesters_roles', '').split(',')
	db.delete('Music.{}:allowed_roles'.format(server_id))
	db.delete('Music.{}:requesters_roles', '').split(',')
	for role in allowed_roles:
		db.sadd('Music.{}:allowed_roles'.format(server_id), role)
	for role in requesters_roles:
		db.sadd('Music.{}:requesters_roles'.format(server_id), role)
	flash('Configuration updated successfully!', 'success')

	return redirect(url_for('plugin_music', server_id=server_id))


@app.route('/request_playlist/<int:server_id>')
def request_playlist(server_id):
	if 'Music' not in db.smembers('plugins:{}'.format(server_id)):
		return redirect(url_for('index'))

	playlist = db.lrange('Music.{}:request_queue'.format(server_id), 0, -1)
	playlist = list(map(lambda v: json.loads(v), playlist))

	is_admin = False
	if session.get('api_token'):
		user = get_user(session['api_token'])
		if not user:
			return redirect(url_for('logout'))
		user_servers = get_user_managed_servers(
			user,
			get_user_guilds(session['api_token'])
		)
		is_admin = str(server_id) in list(map(lambda s: s['id'], user_servers))

	server = {
		'id' : server_id,
		'icon' : db.get('server:{}:icon'.format(server_id)),
		'name' : db.get('server:{}:name'.format(server_id))
	}

	return render_template('request-playlist.html', playlist=playlist,
							server=server, is_admin=is_admin)


@app.route('/delete_request/<int:server_id>/<int:pos>')
@plugin_method
def delete_request(server_id, pos):
	playlist = db.lrange('Music.{}:request_queue'.format(server_id), 0, -1)
	if pos < len(playlist):
		del playlist[pos]
		db.delete('Music.{}:request_queue'.format(server_id))
		for vid in playlist:
			db.rpush('Music.{}:request_queue'.format(server_id), vid)


@app.before_first_request
def setup_logging():
	# In production mode, add log handler to sys.stderr.
	app.logger.addHandler(logging.StreamHandler())
	app.logger.setLevel(logging.INFO)


if __name__ == '__main__':
	app.debug = True
	from os import path

	extra_dirs = ['templates']
	extra_files = extra_dirs[:]
	for extra_dir in extra_dirs, files in os.walk(extra_dir):
		for filename in files:
			filename = path.join(dirname, filename)
			if path.isfile(filename):
				extra_files.append(filename)

	app.run(extra_files=extra_files)