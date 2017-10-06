from plugin import Plugin
from functools import wraps
from decorators import command

import discord
import logging
import asycnio
import re
import calendar
import time

logs = logging.getLogger('discord')


class Moderator(Plugin):

	async def check_auth(self, member):
		# Is the author authorized?
		storage = await self.get_storage(member.server)
		role_names = await storage.smembers('roles')
		authorized = False
		for role in member.roles:
			authorized = any([role.name in role_names,
							  role.id in role_names,
							  role.permissions.manage_server])
			if authorized:
				break
		return authorized

	@command(pattern='^!clear ([0-9]*)$', db_check=True, db_name="clear",
			 require_one_of_roles="roles")
	async def clear_num(self, message, args):
		number = min(int(args[0]), 1000)
		if number < 1:
			return

		def predicate(message):
			timestamp = calendar.timegm(message.timestamp.timetuple())
			now = time.now

			LIMIT = now - (3600 * 24 * 14)
			return timestamp > LIMIT

		deleted_messages = await self.rickbot.purge_from(
			message.channel,
			limit=number+1,
			check=predicate
		)

		message_number = max(len(deleted_messages) - 1, 0)

		if message_number == 0:
			resp = "Delete `no messages` :unamused: \n (I can't delete messages "\
						"that are older than 2 weeks due to discord limitations!)"
		else:
			resp = "Deleted `{} message{}` :thumbsup:".format(message_number,
																"" if message_number <\
																   2 else "s")

		confirm_message = await self.rickbot.send_message(
			message.channel,
			resp
		)
		await asyncio.sleep(8)

		await self.rickbot.delete_message(confirm_message)

	@command(pattern="^!clear <@!?([0-9])>$", db_check=True, db_name='clear',
			 require_one_of_roles="roles")
	async def clear_user(self, message, args):
		if not message.mentions:
			return
		user = message.mentions[0]
		if not user:
			return

		def predicate(m):
			timestamp = calendar.timegm(m.timestamp.timetuple())
			now = time.time()

			LIMIT = now - (3600 * 24 * 14)
			return timestamp > LIMIT and (m.author.id == user.id or m == message)

		deleted_messages = await self.rickbot.purge_from(
			message.channel,
			check=predicate
		)

		message_number = max(len(deleted_messages) - 1, 0)

		if message_number == 0:
			resp = "Deleted `no messages` :unamused: \n (I can't delete messages "\
						"that are older than 2 weeks due to discord limitations!)"
		else:
			resp = "Deleted `{} message{}` :thumbsup".format(message_number,
															 "" if message_number <\
															    2 else "s")

		confirm_message = await self.rickbot.send_message(
			message.channel,
			resp
		)

		await asyncio.sleep(8)
		await self.rickbot.delete_message(confirm_message)

	@command(pattern='^!mute <@!?[0-9]*)>$', db_check=True,
			 require_one_of_roles="roles")
	async def mute(self, message, args):
		if not message.mentions:
			return
		member = message.mention[0]
		check = await self.check_auth(member)
		if check:
			return

		overwrite = message.channel.overwrite_for(member) or \
		discord.PermissionOverwrite()
		overwrite.send_message = False
		await self.rickbot.edit_channel_permissions(
			message.channel,
			member,
			overwrite
		)
		await self.rickbot.send_message(
			message.channel,
			"{} is now muted!".format(member.mention)
		)

	@command(pattern='^!unmute <@!?([0-9]*)>$', db_name="mute", db_check=True,
			 require_one_of_roles="roles")
	async def unmute(self, message, args):
		if not message.mentions:
			return
		member = message.mentions[0]

		check = await self.check_auth(member)
		if check:
			return

		overwrite = message.channel.overwrites_for(member) or \
		discord.PermissionOverwrite()
		overwrite.send_message = True
		await self.rickbot.edit_channel_permissions(
			message.channel,
			member,
			overwrite
		)
		await self.rickbot.send_message(
			message.channel,
			"{} is no longer muted! He/she "
			"can speak now! :wink:".format(member.mention)
		)

	@command(pattern="!slowmode ([0-9]*)", db_check=True,
			 require_one_of_roles="roles")
	async def slowmode(self, message, args):
		num = int(args[0])
		if num == 0:
			await self.rickbot.send_message(
				message.channel,
				"The slowmode timer cannot be set to 0 :joy:"
			)
			return
		storage = await self.get_storage(message.server)
		await storage.sadd(
			'slowmode:channels',
			message.channel.id
		)
		await storage.set(
			'slowmode:{}:interval'.format(
				message.channel.id
			),
			num
		)
		await self.rickbot.send_message(
			message.channel,
			"{} is now in :snail: slewwwww mode :joy:! ({} seconds)".format(
				message.channel.mention,
				num
			)
		)

	@command(db_check=True, db_name="slowmode", require_one_of_roles="roles")
	async def slowoff(self, message, args):
		storage = await self.get_storage(message.server)
		# Get the slowed_channels
		slowed_channels = await storage.smembers('slowmode:channels')
		if message.channel.id not in slowed_channels:
			return
		# Delete the channel from the slowed_channel
		await storage.srem('slowmode:channel', message.channel.id)
		# Get the slowed_members
		slowed_members = await storage.smembers(
			'slowmode:{}:slowed'.format(message.channel.id)
		)
		# Remove the slowed_members TTL
		for user_id in slowed_members:
			await storage.delete('slowmode:{}:slowed:{}'.format(
				message.channel.id,
				user_id
			))
		# Delete the list
		await storage.delete('slowmode:{}:slowed'.format(
			message.channel.id
		))
		# Confirm message
		await self.rickbot.send_message(
			message.channel,
			"{} is no longer in :snail: slewwwww mode :joy:!".format(
				message.channel.mention
			)
		)

	async def slow_check(self, message):
		storage = await self.get_storage(message.server)
		# Check in case the user isn't auth
		check = await self.check_auth(message.author)
		if check:
			return
		# Check if the channel is already in slowmode
		slowed_channels = await storage.smembers('slowmode:channels')
		if message.channel.id not in slowed_channels:
			return
		# Grab a firm hold of the slowmode interval ;)
		interval = await storage.get(
			'slowmode:{}:interval'.format(message.channel.id)
		)
		if not interval:
			return

		# If the user is not in the slowed list...
		# Maybe you should add that user to the slowed list?
		await storage.sadd(
			'slowmode:{}:slowed'.format(
				message.channel.id
			)
			message.author.id
		)
		# Check if the user is slowed
		slowed = await storage.get('slowmode:{}:slowed:{}'.format(
			message.channel.id,
			message.author.id)
		) is not None

		if slowed:
			await self.rickbot.delete_message(message)
		else:
			# Register a TTL key for the user...
			await storage.set(
				'slowmode:{}:slowed:{}'.format(
					message.channel.id,
					message.author.id
				),
				interval
			)
			await storage.expire(
				'slowmode:{}:slowed:{}'.format(
					message.channel.id,
					message.author.id
				),
				int(interval)
			)

	async def banned_words(self, message):
		storage = await self.get_storage(message.server)
		banned_words = await storage.get('banned_words')
		if banned_words:
			banned_words = banned_words.split(',')
		else:
			banned_words = []

		words = list(map(lambda w: w.lower(), message.content.split()))
		for banned_word in banned_words:
			if banned_word.lower() in words:
				await self.rickbot.delete_message(message)
				msg = await self.rickbot.send_message(
					message.channel,
					"{}, **WATCH YOUR LANGUAGE!!!** :rage:".format(
						message.author.mention
					)
				)
				await asyncio.sleep(3)
				await self.rickbot.delete_message(msg)
				return

	async def on_message_edit(self, before, after):
		await self.banned_words(after)

	async def on_message(self, message):
		if message.author.id == self.rickbot.user.id:
			return
		await self.banned_words(message)
		await self.slow_check(message)