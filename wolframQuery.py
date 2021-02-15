import yaml
from pprint import pprint
import requests
import urllib.parse

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.load(ymlfile)

appid = botConfig['WOLFRAM_APPID']
def queryWolfram(input):
    if input == None:
        input = "lifespan of a mosquito"
    query = urllib.parse.quote_plus(input)
    query_url = f"http://api.wolframalpha.com/v2/query?" \
                f"appid={appid}" \
                f"&input={query}" \
                f"&format=plaintext" \
                f"&output=json"

    r = requests.get(query_url).json()
    results = ""
    try:
        r["queryresult"]["pods"]
    except:
        results = "I can't understand you"
        return (results)
    for pod in r["queryresult"]["pods"]:
        results += "\n##" + pod["title"] + "\n" + pod["subpods"][0]["plaintext"] + "\n"
    #data = r["queryresult"]["pods"][0]["subpods"][0]
    #datasource = ", ".join(data["sources"]["source"])
    #microsource = data["microsources"]["microsource"]
    #plaintext = data["plaintext"]

    return (results)