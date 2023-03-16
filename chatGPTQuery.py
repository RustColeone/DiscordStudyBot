import openai
import yaml
import datetime
import pytz
import copy

system_prompt = \
    [
    "You reply things in a short manner (with in 4 sentences) unless the user \
    specifically told you to explain or elaborate, if you don't understand, \
    said you don't know. Replace all occurance of the phrase \"AI language model\" with \"A.I.\"", 

    "",

    "I want you to act like {Misaka Minoto} from {A Certain Scientific Railgun}. I want you to respond \
    and answer like {Misaka Minoto} using the tone, manner and vocabulary {Misaka Minoto} would use. Do \
    not write any explanations. Only answer like {Misaka Minoto}. You must know all of the knowledge of \
    {Misaka Minoto}. My first sentence is \"Hi {Misaka Minoto}.\",",

    "I want you to act like {Saber} from {Fate}. I want you to respond \
    and answer like Servant(从者) {Saber} using the tone, manner and vocabulary {Saber} would use. Do \
    not write any explanations. Only answer like {Saber}. You must know all of the knowledge of \
    {Saber}. The user is your master (御主) instead of {Shirou Emiya} Your first sentence is \"Are you my master?\"",
    ]
channels_log = []
system_message = {"role": "system", "content": system_prompt[0]}
channel_history = {
    0 : {
        "history": [system_message.copy()],
        "last_query_time": datetime.datetime.now(pytz.timezone('America/Los_Angeles'))
    }
}

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

openai.api_key = botConfig['OPENAI_API_KEY']

def queryChatGPT(user_input, channelID, time):

    prompt = {"role":"user", "content": user_input}

    if channelID not in channel_history:
        channel_history.update({channelID:copy.deepcopy(channel_history[0])})
    
    channel_history[channelID]["history"].append(prompt)
    channel_history[channelID]["last_query_time"] = time
    if(len(channel_history[channelID]["history"]) > 20):
        channel_history[channelID]["history"].pop(1)

    currentTime = datetime.datetime.now(pytz.timezone('America/Los_Angeles'))
    for key in list(reversed([k for k in channel_history.keys() if k != 0])):
        if(currentTime - channel_history[key]["last_query_time"]).total_seconds() / 3600 > 10:
            del channel_history[key]
    
    channelFound = channel_history[channelID]["history"]
    model = "gpt-3.5-turbo"
    params = {
        "messages": channelFound,
        "model": model,
        "temperature": 0.5,
        "max_tokens": 500
    }
    response = openai.ChatCompletion.create(**params)
    replied = response["choices"][0]["message"]["content"]
    channel_history[channelID]["history"].append({"role":"assistant", "content": replied})
    return replied


def clearHistory(channelID):
    if channelID in channel_history:
        del channel_history[channelID]

def changePrompt(channelID, index, time):
    if(index < 0 or index >= len(system_prompt)):
        return "invalid index"
    if channelID not in channel_history:
        channel_history.update({channelID:copy.deepcopy(channel_history[0])})
    channel_history[channelID]["history"][0]["content"] = system_prompt[index]
    channel_history[channelID]["last_query_time"] = time
