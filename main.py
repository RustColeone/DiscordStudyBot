import discord
import yaml
import os
import json
from discord.ext import commands
from datetime import datetime
import asyncio
import pytz
import ascii
import wolframQuery
import unifiedChat
import googleQuery
import pathlib
import database as db
from commandParsers import (
    parse_chat_command, parse_music_command, parse_wolfram_command, 
    parse_google_command, parse_db_command, parse_reminder_command
)

from wolframclient.evaluation import WolframLanguageSession
from wolframclient.language import wl, wlexpr

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

# Override with environment variables if they exist
for key in ["TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "SERP_API_KEY", "WOLFRAM_APPID", "WOLFRAM_PATH"]:
    env_value = os.getenv(key)
    if env_value:
        botConfig[key] = env_value

session = WolframLanguageSession(botConfig["WOLFRAM_PATH"])
bot = discord.Client(intents=discord.Intents.all())

timeZoneUTC = pytz.utc
timeZoneCNBJ = pytz.timezone("Asia/Shanghai")
timeZoneUSCA = pytz.timezone("America/Los_Angeles")
timeZoneUKLD = pytz.timezone("Europe/London")

music_list_path = 'musicList.txt'

# Initialize playlist with error handling
try:
    f = open(music_list_path, 'r', encoding='utf8').read()
    playList = f.split('\n')
    playList = [x for x in playList if x != ""]
    if len(playList) > 0 and playList[0].isdigit():
        song_index = int(playList[0])
    else:
        song_index = 1
except FileNotFoundError:
    playList = ["1"]
    song_index = 1
    print("musicList.txt not found. Will be created on first music stop.")

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
	msgChannel = "Private/"
	if message.guild is not None:
		msgChannel = message.guild.name
	if isinstance(message.channel, discord.DMChannel):
		msgChannel = msgChannel + "/DM"
	elif message.channel is not None : 
		msgChannel = msgChannel + "/" + message.channel.name
	print("From " + msgChannel + ", by " + message.author.name + ": \"" + msgText + "\"" )
	if message.author == bot.user:
		return
	
	# Check if listen mode is enabled and message is not a command
	if not msgText.startswith('$'):
		settings = db.get_channel_settings(str(message.channel.id))
		if settings["listen_mode"]:
			# Pass message to LLM
			ans = unifiedChat.query_chat(msgText, message.channel.id, message.created_at)
			await message.channel.send(ans)
			return
	
	# Command handling
	if msgText.startswith('$help'):
		with open('help.txt', 'r', encoding='utf-8') as f:
			help_content = f.read()
		
		# Check if user wants detailed help for a specific topic
		parts = msgText.split(maxsplit=1)
		if len(parts) == 2:
			topic = parts[1].lower().strip()
			# Extract detailed section
			if '---DETAILED-HELP---' in help_content:
				detailed_section = help_content.split('---DETAILED-HELP---')[1]
				section_marker = f'=== {topic.upper()} ==='
				
				if section_marker in detailed_section:
					# Extract the specific section
					sections = detailed_section.split('===')
					for i, section in enumerate(sections):
						if section.strip().startswith(topic.upper()):
							# Get this section (current + next text until next ===)
							section_text = '===' + section
							if i + 1 < len(sections):
								# Find where next section starts
								next_section_start = detailed_section.find('===', detailed_section.find(section_marker) + len(section_marker))
								if next_section_start != -1:
									section_text = detailed_section[detailed_section.find(section_marker):next_section_start]
								else:
									section_text = detailed_section[detailed_section.find(section_marker):]
							else:
								section_text = detailed_section[detailed_section.find(section_marker):]
							
							await message.channel.send(f"```md\n{section_text.strip()}\n```")
							return
				
				# Topic not found
				await message.channel.send(f"Topic '{topic}' not found. Available: chat, music, search, time, db")
				return
		
		# Show trimmed version (everything before ---DETAILED-HELP---)
		if '---DETAILED-HELP---' in help_content:
			help_text = help_content.split('---DETAILED-HELP---')[0].strip()
		else:
			help_text = help_content
		
		await message.channel.send(f"```md\n{help_text}\n```")
	elif msgText.startswith('$time'):
		timeCNBJ, timeUSCA, timeUKLD, timerM, timerS = get_time_zone_info()
		text = built_clock_string(timeCNBJ, timeUSCA, timeUKLD, timerM, timerS)
		await message.channel.send(text)
	elif msgText.startswith('$start'):
		await message.channel.send('Starting Clock')
		bot.loop.create_task(get_time_info(channel))
	elif msgText.startswith('$stop'):
		await message.channel.send('Stopping Timer')
		bot.loop.stop()
	elif msgText.startswith('$remindMeIn'):
		await message.channel.send('Timer Set')
		parameters = message.content.split(" ")
		timerMinutes = 5
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
	elif msgText.startswith('$broadcast'):
		await channel1.send(message.content.replace('/broadcast ',''))
	elif msgText.startswith('$music'):
		# Parse the music command
		cmd = parse_music_command(msgText)
		if cmd.errors:
			await message.channel.send("❌ " + "\n".join(cmd.errors))
			return
		
		action = cmd.action
		if action == "playTest":
			await playMusicTest(voice_channel)
			await message.channel.send('Music Started testing')
		elif action == "init":
			# Check if user is in a voice channel
			if message.author.voice is None:
				await message.channel.send('You need to be in a voice channel first!')
				return
			
			user_voice_channel = message.author.voice.channel
			voice_chat = await user_voice_channel.connect()
			
			# Save music state to database
			db.save_music_state(str(message.channel.id), str(user_voice_channel.id), song_index, False)
			
			selectMusic(clamp(song_index))
			await message.channel.send(f'Music player initialized in {user_voice_channel.name}')
		elif action == "stop":
			if voice_chat is None:
				await message.channel.send('Music player is not initialized')
				return
			exitMusic()
			
			# Clear music state from database
			db.clear_music_state(str(message.channel.id))
			
			await voice_chat.disconnect()
			await message.channel.send('Music player stopped')
		elif action == "name":
			await message.channel.send('Music player is playing: #{} {}'.format(song_index, song_current))
			if voice_chat and voice_chat.is_connected():
				await playMusic(True)
		elif action == "play":
			if voice_chat is None or not voice_chat.is_connected():
				await message.channel.send('Music player is not initialized. Use $music initialize first')
				return
			
			# Update music state
			db.save_music_state(str(message.channel.id), str(voice_chat.channel.id), song_index, True)
			
			await message.channel.send('Music player started')
			await playMusic(True)
		elif action == "pause":
			if voice_chat is None or not voice_chat.is_connected():
				await message.channel.send('Music player is not initialized')
				return
			
			# Update music state
			db.save_music_state(str(message.channel.id), str(voice_chat.channel.id), song_index, False)
			
			await message.channel.send('Music player paused')
			await playMusic(False)
		elif action == "next":
			if voice_chat is None or not voice_chat.is_connected():
				await message.channel.send('Music player is not initialized')
				return
			await message.channel.send('Music player is now playing the next song')
			await playMusic(False)
			next()
			await playMusic(True)
		elif action == "prev":
			if voice_chat is None or not voice_chat.is_connected():
				await message.channel.send('Music player is not initialized')
				return
			await message.channel.send('Music player is now playing the previous song')
			await playMusic(False)
			previous()
			await playMusic(True)
	
	elif msgText.startswith('$wolfram'):
		cmd = parse_wolfram_command(msgText)
		if cmd.errors:
			await message.channel.send("❌ " + "\n".join(cmd.errors))
			return
		if not cmd.query:
			await message.channel.send("❌ No query provided")
			return
		
		ans = wolframQuery.queryWolfram(cmd.query)
		tempText = "Wolfram Replied>\n```md\n" + ans + "\n```"
		await message.channel.send(tempText)
	
	elif msgText.startswith('$google'):
		cmd = parse_google_command(msgText)
		if cmd.errors:
			await message.channel.send("❌ " + "\n".join(cmd.errors))
			return
		if not cmd.query:
			await message.channel.send("❌ No query provided")
			return
		
		tempText = "Google Replied>\n"
		resultTitle, resultLink, resultDescription = googleQuery.queryGoogle(cmd.query)
		embed=discord.Embed(title="Googled results of {}".format(cmd.query))
		description = ""
		for i in range(len(resultTitle)):
			description += "[{}]({})\n{}\n\n".format(resultTitle[i], resultLink[i], resultDescription[i])
		embed.description = description
		await message.channel.send(tempText, embed = embed)
	
	# ==================== Unified Chat Commands (Professional CLI Style) ====================
	elif msgText.startswith('$chat'):
		# Parse the command
		cmd = parse_chat_command(msgText)
		
		# Report errors if any
		if cmd.errors:
			error_msg = "❌ **Command Errors:**\n" + "\n".join([f"• {err}" for err in cmd.errors])
			await message.channel.send(error_msg)
			return
		
		responses = []
		
		# Process in order: LLM -> Model -> Prompt -> Clear -> Listen -> Send -> Status/Models
		
		# 1. Set LLM
		if cmd.llm:
			result = unifiedChat.set_llm(message.channel.id, cmd.llm)
			responses.append(result)
		
		# 2. Set Model
		if cmd.model:
			result = unifiedChat.set_model(message.channel.id, cmd.model)
			responses.append(result)
		
		# 3. Handle Prompt Actions
		if cmd.prompt_action:
			if cmd.prompt_action == 'list':
				responses.append(unifiedChat.get_prompt_list())
			elif cmd.prompt_action == 'show':
				llm, prompt_content = unifiedChat.show_prompt(message.channel.id)
				if prompt_content:
					responses.append(f"**Current prompt for {llm}:**\n```\n{prompt_content}\n```")
				else:
					with open('llm_config.json', 'r', encoding='utf-8') as f:
						default_prompt = json.load(f)['prompts'][0]
					responses.append(f"**Using default prompt:** {default_prompt['name']}\n```\n{default_prompt['content']}\n```")
			elif cmd.prompt_action == 'set':
				unifiedChat.set_custom_prompt(message.channel.id, cmd.prompt_value)
				responses.append("✅ Custom prompt set!\n💡 Consider using `$chat --clear` to start fresh")
			elif cmd.prompt_action.isdigit():
				index = int(cmd.prompt_action)
				result = unifiedChat.change_prompt(message.channel.id, index, message.created_at)
				responses.append(result)
		
		# 4. Clear history
		if cmd.clear_history:
			unifiedChat.clear_history(message.channel.id)
			responses.append("✅ Chat history cleared")
		
		# 5. Toggle listen mode
		if cmd.toggle_listen:
			new_state = db.toggle_listen_mode(str(message.channel.id))
			if new_state:
				responses.append("🟢 **Listen mode enabled**\nI'll respond to all your messages (except $ commands)")
			else:
				responses.append("🔴 **Listen mode disabled**\nUse `$chat --send <message>` to chat")
		
		# 6. Send message
		if cmd.message:
			ans = unifiedChat.query_chat(cmd.message, message.channel.id, message.created_at)
			responses.append(ans)
		
		# 7. Show models list
		if cmd.show_models:
			responses.append(unifiedChat.get_models_list())
		
		# 8. Show status (if no other action or explicitly requested)
		if cmd.show_status and not (cmd.message or cmd.show_models or cmd.prompt_action == 'list'):
			responses.append(unifiedChat.get_status(message.channel.id))
		
		# Send all responses
		if responses:
			await message.channel.send("\n\n".join(responses))
		else:
			await message.channel.send(unifiedChat.get_status(message.channel.id))
	
	elif msgText.startswith('$db'):
		cmd = parse_db_command(msgText)
		if cmd.errors:
			await message.channel.send("❌ " + "\n".join(cmd.errors))
			return
		
		if cmd.action == 'export':
			filepath = db.export_to_json()
			stats = db.get_database_stats()
			await message.channel.send(f"📦 Database exported to {filepath}\nStats: {stats['total_messages']} messages, {stats['active_music_sessions']} music sessions")
		elif cmd.action == 'import':
			try:
				db.import_from_json()
				await message.channel.send("✅ Database imported successfully")
			except Exception as e:
				await message.channel.send(f"❌ Import failed: {str(e)}")
		elif cmd.action == 'stats':
			stats = db.get_database_stats()
			msg_by_ai = ", ".join([f"{k}: {v}" for k, v in stats['messages_by_ai'].items()])
			await message.channel.send(f"📊 Database Stats:\nTotal messages: {stats['total_messages']}\nBy AI: {msg_by_ai}\nMusic sessions: {stats['active_music_sessions']}")
		

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
	tempText += "#" + "#" * 33 + "\n"
	tempText += "#" + " " * 32 + "#\n"

	for i in range(5):
		text[i] = "# "
		if timerM < 10:
			text[i] += ascii.numbers[0][i] + ascii.numbers[timerM][i]
		else:
			text[i] += ascii.numbers[int(timerM / 10)][i] + ascii.numbers[timerM % 10][i]
		text[i] += ascii.coloum[i]
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
	return tempText

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
	if voice_chat and voice_chat.is_connected():
		voice_chat.play(discord_music, after=lambda e: next())
	else:
		print("Cannot Connect - voice_chat is None or not connected")
	#voice_chat.pause()

async def playMusic(should_play):
	global voice_chat
	if voice_chat is None or not voice_chat.is_connected():
		print("Voice chat not connected")
		return
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
	for i in range(1, len(playList)):
		f.write("\n"+playList[i])
	f.close()

#print("Token is", botConfig["TOKEN"])
bot.run(botConfig["TOKEN"])