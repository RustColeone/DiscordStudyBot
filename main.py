import discord
import yaml
from discord.ext import commands
from datetime import datetime
import asyncio
import pytz
import ascii
import wolframQuery
import googleQuery
import pathlib

from wolframclient.evaluation import WolframLanguageSession
from wolframclient.language import wl, wlexpr

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.load(ymlfile)

session = WolframLanguageSession(botConfig["WOLFRAM_PATH"])
bot = discord.Client(command_prefic = '$')

timeZoneUTC = pytz.utc
timeZoneCNBJ = pytz.timezone("Asia/Shanghai")
timeZoneUSCA = pytz.timezone("America/Los_Angeles")
timeZoneUKLD = pytz.timezone("Europe/London")

music_list_path = 'musicList.txt'

f = open(music_list_path, 'r', encoding='utf8').read()
playList = f.split('\n')
playList = [x for x in playList if x != ""]

song_index = int(playList[0])
song_current = ""
discord_music = None
channel = None
channel1 = None
voice_channel = None
voice_chat = None
playNext = True

@bot.event
async def on_ready():
	global voice_channel
	global channel
	global channel1
	print('We have logged in as {0.user}'.format(bot))
	voice_channel = bot.get_channel(botConfig["ID_VOICECHANNEL"])
	channel = bot.get_channel(botConfig["ID_CHANNEL"])
	channel1 = bot.get_channel(botConfig["ID_CHANNEL1"])
@bot.event
async def on_message(message):
	global voice_chat
	msgText = message.content

	if message.author == bot.user:
		return
	elif msgText.startswith('/help'):
		help = "```" \
			+ "Start Clock    => /start\n"\
			+ "Stopp Clock    => /stop\n"\
			+ "Print time     => /time\n"\
			+ "Remind         => /remindMeIn <minutes> <msg>\n"\
			+ "Play Music     => /music initialize\n"\
			+ "          play =>        play\n"\
			+ "         pause =>        pause\n"\
			+ "      get name =>        name\n"\
			+ "     next song =>        next\n"\
			+ "     last song =>        previous\n"\
			+ "Type Set math  => /typeSetMath <equation>\n"\
			+ "Search Wolfram => /wolfram <query>\n"\
			+ "Search Google  => /google <query>\n"\
			+ "```"
		await message.channel.send(help)
	elif msgText.startswith('/time'):
		timeCNBJ, timeUSCA, timeUKLD, timerM, timerS = get_time_zone_info()
		text = built_clock_string(timeCNBJ, timeUSCA, timeUKLD, timerM, timerS)
		await message.channel.send(text)
	elif msgText.startswith('/start'):
		await message.channel.send('Starting Clock')
		bot.loop.create_task(get_time_info(channel))
	elif msgText.startswith('/stop'):
		await message.channel.send('Stopping Timer')
		bot.loop.stop()
	elif msgText.startswith('/remindMeIn'):
		await message.channel.send('Timer Set')
		parameters = message.content.split(" ")
		timerMinutes = 5;
		try:
			timerMinutes = float(parameters[1])
		except:
			await message.channel.send('defaulting to 5 minutes')
		msg = ""
		try:
			del parameters[0:2]
			for par in parameters:
				msg += par + " "
		except:
			await message.channel.send('without messages')
		bot.loop.create_task((remind_me_in(timerMinutes, message.author, msg)))
	elif msgText.startswith('/broadcast'):
		await channel1.send(message.content.replace('/broadcast ',''))
	elif msgText.startswith('/music'):
		parameters = message.content.split(" ")
		if parameters[1] == "playTest":
			await playMusicTest(voice_channel)
			await message.channel.send('Music Started testing')
		elif parameters[1] == "initialize":
			voice_chat = await voice_channel.connect()
			selectMusic(clamp(song_index))
			await message.channel.send('Music player initialized')
		elif parameters[1] == "stop":
			exitMusic()
			await voice_chat.disconnect()
			await message.channel.send('Music player stopped')
		elif parameters[1] == "name":
			await message.channel.send('Music player is playing: #{} {}'.format(song_index, song_current))
			await playMusic(True)
		elif parameters[1] == "play":
			await message.channel.send('Music player started')
			await playMusic(True)
		elif parameters[1] == "pause":
			await message.channel.send('Music player paused')
			await playMusic(False)
		elif parameters[1] == "next":
			await message.channel.send('Music player is now playing the next song')
			await playMusic(False)
			next()
			await playMusic(True)
		elif parameters[1] == "previous":
			await message.channel.send('Music player is now playing the previous song')
			await playMusic(False)
			previous()
			await playMusic(True)
	elif msgText.startswith('/typeSetMath'):
		ans = session.evaluate(wlexpr('ToString[' + msgText[12:] + ']'))
		tempText = "```md\n" + ans + "\n```"
		await message.channel.send(tempText)
	elif msgText.startswith('/wolfram'):
		ans = wolframQuery.queryWolfram(msgText[8:])
		tempText = "Wolfram Replied>\n```md\n" + ans + "\n```"
		await message.channel.send(tempText)
	elif msgText.startswith('/google'):
		tempText = "Google Replied>\n"
		resultTitle, resultLink, resultDescription = googleQuery.queryGoogle(msgText[7:])
		embed=discord.Embed(title="Googled results of {}".format(msgText[7:]))
		description = ""
		for i in range(len(resultTitle)):
			description += "[{}]({})\n{}\n\n".format(resultTitle[i], resultLink[i], resultDescription[i])
		embed.description = description
		await message.channel.send(tempText, embed = embed)

async def get_time_info(channel):
	#message = await channel.fetch_message(botConfig["ID_MESSAGE"])
	message = await channel.send("Starting Clock")
	timeUTC = datetime.now(pytz.utc).strftime('%y/%m/%d %H:%M')
	timerS = 0
	while True:
		timeCNBJ, timeUSCA, timeUKLD, timerM, timerS = get_time_zone_info()
		if(timerS % 5 == 0):
			tempText = built_clock_string(timeCNBJ, timeUSCA, timeUKLD, timerM, timerS)
			await message.edit(content=tempText)

def get_time_zone_info():
	timeCNBJ = datetime.now(timeZoneCNBJ).strftime('CN-BJ> %y/%m/%d %H:%M:%S\n')
	timeUSCA = datetime.now(timeZoneUSCA).strftime('US-LA> %y/%m/%d %H:%M:%S\n')
	timeUKLD = datetime.now(timeZoneUKLD).strftime('UK-LD> %y/%m/%d %H:%M:%S')
	timerM = int(datetime.now(pytz.utc).strftime('%M'))
	timerS = int(datetime.now(pytz.utc).strftime('%S'))
	return timeCNBJ, timeUSCA, timeUKLD, timerM, timerS

def built_clock_string(timeCNBJ, timeUSCA, timeUKLD, timerM, timerS):
	text = [None] * 5
	codeblock = "```"
	tempText = codeblock
	breakTimeText = "Break Time"
	if(timerM < 45): 
		tempText += "md\n"
		breakTimeText = "Not Break Time just Yet"
	tempText += "#" + "#" * 33 + "\n";
	tempText += "#" + " " * 32 + "#\n";

	for i in range(5):
		text[i] = "# "
		if timerM < 10:
			text[i] += ascii.numbers[0][i] + ascii.numbers[timerM][i]
		else:
			text[i] += ascii.numbers[int(timerM / 10)][i] + ascii.numbers[timerM % 10][i]
		text[i] += ascii.coloum[i];
		if timerS < 10:
			text[i] += ascii.numbers[0][i] + ascii.numbers[timerS][i]
		else:
			text[i] += ascii.numbers[int(timerS / 10)][i] + ascii.numbers[timerS % 10][i]
		tempText += text[i] + "#\n"
	tempText += "#" + " " * 32 + "#\n"
	tempText += "#" + "#" * 33 + "\n"
	tempText += codeblock + "\n"
	tempText += breakTimeText
	tempText += codeblock + "ml\n"
	tempText += "Last updated in \n" + timeCNBJ + timeUSCA + timeUKLD + codeblock
	return tempText;

async def remind_me_in(minutes, member, message):
	if(message != ""):
		message = "to [ " + message + "] "
	await asyncio.sleep(minutes * 60)
	await member.send('boop, you told me to remind you {} {} minutes ago'.format(message, minutes))

async def playMusicTest(voice_channel):
	if voice_channel != None:
		vc = await voice_channel.connect()
		vc.play(discord.FFmpegPCMAudio(source = "music\\EVA_OP.mp3"), after=lambda e: print("done"))
		while vc.is_playing():
			await asyncio.sleep(0.5)
		await vc.disconnect()

def selectMusic(index):
	global song_current
	global song_index
	global discord_music
	global voice_chat

	song_index = index
	song_current = playList[song_index]
	#print(song_index)
	discord_music = discord.FFmpegPCMAudio(source = "music\\" + song_current)
	if voice_chat.is_connected():
		voice_chat.play(discord_music, after=lambda e: next())
	else:
		print("Cannot Connect")
	#voice_chat.pause()

async def playMusic(should_play):
	global voice_chat
	if should_play:
		voice_chat.resume()
	else:
		voice_chat.pause()
	#voice_chat.is_playing()

def next():
	global song_index
	song_index += 1
	selectMusic(clamp(song_index))

def previous():
	global song_index
	song_index -= 1
	selectMusic(clamp(song_index))

def clamp(number):
	length = len(playList)
	if (number < 1):
		number = number + length - 1
	if (number >= len(playList)):
		number = number - length + 1
	return number

def exitMusic():
	f = open(music_list_path, 'w', encoding='utf8')
	f.write(str(song_index))
	for i in range(1, len(playList) - 1):
		f.write("\n"+playList[i])

#print("Token is", botConfig["TOKEN"])
bot.run(botConfig["TOKEN"])