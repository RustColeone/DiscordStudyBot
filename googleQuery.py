from googleapi import google, images
def queryGoogle(input):
    num_page = 5
    search_results = google.search(input, num_page)
    resultTitle = []
    resultLink = []
    resultDescription = []
    i = 0
    options = images.ImageOptions()
    for e in search_results:
        resultTitle.append("##" + search_results[i].name)
        resultLink.append(search_results[i].link)
        resultDescription.append(search_results[i].description )
        i += 1
        if(i >= 5):
            break
    return resultTitle, resultLink, resultDescription