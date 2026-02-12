import os
import requests
import yaml

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

# Use environment variable if available, otherwise use config
# You can get a free SerpAPI key at https://serpapi.com/
serp_api_key = os.getenv('SERP_API_KEY') or botConfig.get('SERP_API_KEY', '')

def queryGoogle(input):
    """
    Query Google using SerpAPI (requires API key from serpapi.com)
    Free tier: 100 searches/month
    """
    if not serp_api_key:
        return ["Error"], [""], ["SERP_API_KEY not configured. Get one at https://serpapi.com/"]
    
    try:
        params = {
            "engine": "google",
            "q": input,
            "api_key": serp_api_key,
            "num": 5
        }
        
        response = requests.get("https://serpapi.com/search", params=params)
        results = response.json()
        
        resultTitle = []
        resultLink = []
        resultDescription = []
        
        if "organic_results" in results:
            for result in results["organic_results"][:5]:
                resultTitle.append("##" + result.get("title", "No title"))
                resultLink.append(result.get("link", ""))
                resultDescription.append(result.get("snippet", "No description"))
        else:
            error_msg = results.get("error", "No results found")
            return ["Error"], [""], [error_msg]
        
        return resultTitle, resultLink, resultDescription
    except Exception as e:
        return ["Error"], [""], [f"Google search failed: {str(e)}"]