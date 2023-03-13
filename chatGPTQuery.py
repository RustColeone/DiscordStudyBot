import openai
import yaml

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

openai.api_key = botConfig['OPENAI_API_KEY']

def queryChatGPT(input):
    prompt = input
    model = "text-davinci-002"
    params = {
        "prompt": prompt,
        "model": model,
        "temperature": 0.5,
        "max_tokens": 50
    }
    response = openai.Completion.create(**params)
    return response["choices"][0]["text"]