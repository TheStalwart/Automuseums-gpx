import requests
from bs4 import BeautifulSoup

print("Downloading country list...")
r = requests.get('https://automuseums.info/homepage')
html_contents = r.text
soup = BeautifulSoup(html_contents, 'html.parser')
countries = soup.find(id='block-searchmuseumsin').find_all('a')
for country in countries:
    print(f"{country['href']}")
