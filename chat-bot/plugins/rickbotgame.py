from plugin import Plugin
import discord
import os


class RickBotGame(Plugin):

	is_global = True
	game = os.getenv("RICKBOT_GAME", 'rickbot.com')

	async def on_ready(self):
		await self.RickBot.change_presence(
			game=discord.Game(
				name=self.game
			)
		)