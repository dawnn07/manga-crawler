from bs4 import BeautifulSoup
import requests
import concurrent.futures
import pymongo
from urllib.parse import urlparse
import re
import os
import time
from dotenv import load_dotenv
from zenrows import ZenRowsClient

load_dotenv()

myclient = pymongo.MongoClient(os.getenv("MONGO_URI"))
mydb = myclient["devtruyen"]
mycol = mydb["comics"]


print(myclient.list_database_names())

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0'})

def rate_limited_request(url, retries=5, backoff_factor=5):
    for attempt in range(retries):
        try:
            response = session.get(url)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            if attempt < retries - 1:
                backoff_time = backoff_factor * (2 ** attempt)
                print(f"Error fetching {url}: {e}. Retrying in {backoff_time} seconds...")
                time.sleep(backoff_time)
            else:
                print(f"Failed to fetch {url} after {retries} attempts.")
                return None

def comic_info(mainpageURL):
    chapterlist = {}
    try:
        response = rate_limited_request(mainpageURL)
        if response is None:
            return None
        
        parsed_html = BeautifulSoup(response.content, 'html.parser')
        detail = {
            "title": parsed_html.find("h1", class_="title-detail").get_text(),
            "banner": urlparse(parsed_html.find("div", class_="col-xs-4 col-image").find("img").get("src")).path,
            "author": parsed_html.find("li", class_="author").find("p", class_="col-xs-8").get_text(),
            "status": parsed_html.find("li", class_="status").find("p", class_="col-xs-8").get_text(),
            "description": parsed_html.find("div", class_="detail-content").find_all("div", style="padding-top: 10px")[1].get_text(),
        }
        genre_list = []
        genre_a_tags = parsed_html.find("li", class_="kind row").find("p", class_="col-xs-8").find_all("a")
        for genre_a in genre_a_tags:
            genre = genre_a.get_text(strip=True)
            genre_list.append(genre)
        detail['genre'] = genre_list

        all_chapter = parsed_html.find("div", class_="list-chapter", id="nt_listchapter").find_all("div", class_="chapter")
        option_select = []
        for chapter_div in all_chapter:
            a_tags = chapter_div.find_all("a")
            option_select.extend(a_tags)
        if option_select:
            for chapter in option_select:
                link_chapter = chapter.get('href')
                title_chapter = chapter.get_text()
                chapterlist.update({title_chapter.upper(): urlparse(link_chapter).path})
        else:
            print("Cannot find manga list")
        detail['episodes'] = chapterlist
    except requests.RequestException as e:
        print(f"Error fetching URL: {e}")
        return None
    print("Comic information retrieved.", detail["description"])
    return {
        'comic_path': urlparse(mainpageURL).path,
        'comic_detail': detail,
        'chapterlist': chapterlist,
    }

def download_chapter(name_chapter, list_chapter):
    url = os.getenv("MANGA_DOMAIN") + list_chapter[name_chapter]
    response = rate_limited_request(url)

    if response is None or response.status_code != 200:
        print(f"Failed to retrieve {url}.")
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    page_chapter = soup.find('div', class_='reading-detail').find_all('div', class_='page-chapter')
    links = []

    for page in page_chapter:
        img = page.find('img')
        if img:
            img_url = img.get('data-src')
            links.append(urlparse(img_url).path)

    print(f"Downloaded {name_chapter}")
    return {
        "chapter": int(re.search(r"CHAPTER (\d+)", name_chapter, re.IGNORECASE).group(1)),
        "images": links
    }

def update_all_comics_in_db():
    comics = mycol.find()
    for comic in comics:
        comic_path = comic['comic_path']
        comic_episodes = comic['comic_detail']['episodes']
        try:
            response = rate_limited_request(os.getenv("MANGA_DOMAIN") + comic_path)
            if response is None:
                continue

            parsed_html = BeautifulSoup(response.content, 'html.parser')
            all_chapter = parsed_html.find("div", class_="list-chapter", id="nt_listchapter").find_all("div", class_="chapter")
            chapterlist = {}
            option_select = []
            for chapter_div in all_chapter:
                a_tags = chapter_div.find_all("a")
                option_select.extend(a_tags)
            if option_select:
                for chapter in option_select:
                    link_chapter = chapter.get('href')
                    title_chapter = chapter.get_text()
                    chapterlist.update({title_chapter.upper(): urlparse(link_chapter).path})
            else:
                print("Cannot find manga list")

            new_chapters = {k: v for k, v in chapterlist.items() if k not in comic_episodes}
            if new_chapters:
                print(f"New chapters found for {comic['comic_detail']['title']}")
                results = []
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    futures = [executor.submit(download_chapter, name_chapter, chapterlist) for name_chapter in new_chapters]
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            result = future.result()
                            if result:
                                results.append(result)
                        except Exception as e:
                            print(f"Error downloading chapter: {e}")
                sorted_results = sorted(results, key=lambda x: x['chapter'], reverse=True)
                mycol.update_one(
                    {'comic_path': comic_path},
                    {'$set': {
                        'chapters': [*sorted_results, *comic['chapters']],
                        'comic_detail': {
                            **comic['comic_detail'],
                            'episodes': {**new_chapters, **comic_episodes}
                        }
                    }}
                )
            else:
                print(f"No new chapters found for {comic['comic_detail']['title']}")
        except requests.RequestException as e:
            print(f"Error fetching URL: {e}")

def download_top_comics(start_page , number_of_pages):
    client = ZenRowsClient(os.getenv("API_KEY"))
    for i in range(start_page, number_of_pages + 1):
        top_all_url = os.getenv("MANGA_DOMAIN") + "tim-truyen?sort=10&status=&page=" + str(i)
        params = {"js_render":"true"}
        response = client.get(top_all_url, params=params)
        if response is None:
            return None
        parsed_html = BeautifulSoup(response.text, 'html.parser')
        comics_tags = parsed_html.find("div", class_="Module-170").find("div", class_="ModuleContent").find("div", class_="items").find_all("div", class_="image")
        comics_link = []
        for comic in comics_tags:
            comic_link = comic.find("a").get("href")
            comics_link.append(comic_link)
        for comic_link in comics_link:
            print(f"Downloading {comic_link}")
            if mycol.find_one({'comic_path': urlparse(comic_link).path}):
                print("Comic already exists in the database.")
                continue
            chapter_data = comic_info(comic_link)
            if chapter_data is None:
                print("Failed to retrieve comic information.")
                continue
            chapters = chapter_data['chapterlist']
            comic_path = chapter_data['comic_path']
            comic_detail = chapter_data['comic_detail']
            results = []
            with concurrent.futures.ThreadPoolExecutor() as executor:
                futures = [executor.submit(download_chapter, name_chapter, chapters) for name_chapter in chapters]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                    except Exception as e:
                        print(f"Error downloading chapter: {e}")
            sorted_results = sorted(results, key=lambda x: x['chapter'], reverse=True)
            mycol.insert_one({
                'comic_path': comic_path,
                'comic_detail': comic_detail,
                'chapters': sorted_results
            })


def get_chapter_list_from_user():
    while True:
        print("SCRIPT DOWNLOAD TRUYEN TRANH")
        print("\nNHAN SO 1 DE DOWNLOAD CHAP TUY CHON")
        print("NHAN SO 2 DE DOWNLOAD HET CAC CHAP")
        print("NHAN SO 3 DE UPDATE LAI DANH SACH TRUYEN")
        print("NHAN SO 4 DE DOWNLOAD TOP TRUYEN HOT")
        print("\nNHAN SO 0 DE THOAT")

        try:
            value = int(input())
        except ValueError:
            print("Invalid input. Please enter a number.")
            continue

        if value == 0:
            print("Exiting the script.")
            break
        elif value == 1:
            url = input("Please enter the URL to get the chapter list: ")
            chapter_data = comic_info(url)
            if chapter_data is None:
                print("Failed to retrieve comic information.")
                continue

            chapters = chapter_data['chapterlist']
            comic_path = chapter_data['comic_path']
            comic_detail = chapter_data['comic_detail']
            print("\nNHAP CHAP BAN MUON DOWNLOAD")
            value_one = input()
            name_chapter = "CHAPTER " + value_one
            print("DANG DOWNLOAD CHAP " + name_chapter)
            if name_chapter in chapters:
                download_chapter(name_chapter, chapters)
            else:
                print("CHAP CHUA RA HOAC BI LOI ROI BAN OI!!!")
        elif value == 2:
            url = input("Please enter the URL to get the chapter list: ")
            chapter_data = comic_info(url)
            if chapter_data is None:
                print("Failed to retrieve comic information.")
                continue

            chapters = chapter_data['chapterlist']
            comic_path = chapter_data['comic_path']
            comic_detail = chapter_data['comic_detail']
            results = []
            with concurrent.futures.ThreadPoolExecutor() as executor:
                futures = [executor.submit(download_chapter, name_chapter, chapters) for name_chapter in chapters]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                    except Exception as e:
                        print(f"Error downloading chapter: {e}")
            sorted_results = sorted(results, key=lambda x: x['chapter'], reverse=True)
            mycol.insert_one({
                'comic_path': comic_path,
                'comic_detail': comic_detail,
                'chapters': sorted_results
            })
        elif value == 3:
            update_all_comics_in_db()
        elif value == 4:
            start_page = input("Nhap trang bat dau download: ")
            number_of_pages = input("Nhap so trang muon download: ")
            download_top_comics(int(start_page),int(number_of_pages))

        else:
            print("BAN NHAP CHUA HOP LE!!!")

get_chapter_list_from_user()