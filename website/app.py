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


def get_mention_parsers(server_id, members=None):
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


@app.route