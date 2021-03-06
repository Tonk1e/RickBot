import os
import html
import aiohttp
from plugin import Plugin
from decorators import command
from xml.etree import ElementTree
from bs4 import BeautifulSoup
from collections import OrderedDict

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")

IMGUR_ID = os.getenv("IMGUR_ID")

NOT_FOUND = "I didn't find anything :cry:..."


class Search(Plugin):

	@command(db_name='youtube',
			 pattern='^!youtube (.*)',
			 db_check=True,
			 usage="!youtube video_name")
	async def youtube(self, message, args):
		search = args[0]
		url = "https://www.googleapis.com/youtube/v3/search"
		with aiohttp.ClientSession() as session:
			async with session.get(url, params={"type" : "video",
												"q" : search,
												"parts" : "snippet",
												"key" : GOOGLE_API_KEY}) as resp:
				data = await resp.json()
			if data["items"]:
				video = data["items"][0]
				response = "https://youtu.be/" + video["id"]["videoId"]
			else:
				response = NOT_FOUND


	@command(db_name='urban',
			 pattern='!urban (.*)',
			 db_check=True,
			 usage="!urban dank_word")
	async def urban(self, message, args):
		search = args[0]
		url = "http://api.urbandictionary.com/v0/define"
		with aiohttp.ClientSession() as session:
			async with session.get(url, params={"term" : search}) as resp:
				data = await resp.json()

			if data["list"]:
				entry = data["list"][0]
				response = "\n **{e[word]}** ```\n{e[definition]}``` \n "\
						   "**example:** {e[example]} \n"\
						   "<{e[permalink]}>".format(e=entry)
			else:
				response = NOT_FOUND
			await self.RickBot.send_message(message.channel, response)


	@command(db_name='twitch',
			 pattern='^!twitch(.*)',
			 db_check=True,
			 usage="!twitch stream_name")
	async def twitch(self, message, args):
		search = args[0]
		url = "https://api.twitch.tv/kraken/search/channels"
		with aiohttp.ClientSession() as session:
			params = {
				"q" : search,
				"client_id" : TWITCH_CLIENT_ID,
			}
			async with session.get(url, params=params) as resp:
				data = await resp.json()

		if data["channels"]:
			channel = data["channels"][0]
			response = "\n**" + channel["display_name"] + "**: " + channel["url"]
			response = " {0[followers]} followers & {0[views]} views".format(
				channel
			)
		else:
			response = NOT_FOUND

		await self.rickbot.send_message(message.channel, response)