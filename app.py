#!/usr/bin/env python3
"""
Movie Showtimes – Boston Area
Today's showtimes from 6 local theaters near Belmont, MA.

Setup (one-time):
  pip3 install flask requests beautifulsoup4 lxml playwright playwright-stealth
  playwright install chromium

Run:
  python3 app.py

Open the Network URL on your iPhone (same WiFi as your Mac).
First load takes ~40s (parallel scraping of 6 sites).
"""

import json
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date as date_type, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)


def _refresh_today(date_str=None):
    """Set the global date variables. If date_str is given (YYYY-MM-DD), use
    that date; otherwise use today."""
    global TODAY_ISO, TODAY_MDY, TODAY_MDY2, TODAY_DOW, TODAY_ABBREV
    if date_str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            d = datetime.now()
    else:
        d = datetime.now()
    TODAY_ISO    = d.strftime("%Y-%m-%d")
    TODAY_MDY    = d.strftime("%B %-d")
    TODAY_MDY2   = d.strftime("%B %d")
    TODAY_DOW    = d.strftime("%A")
    TODAY_ABBREV = d.strftime("%b %-d")


# initialise once at import
TODAY_ISO = TODAY_MDY = TODAY_MDY2 = TODAY_DOW = TODAY_ABBREV = ""
_refresh_today()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get(url, timeout=15, **kwargs):
    return requests.get(url, headers=HEADERS, timeout=timeout, **kwargs)


def clean(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def truncate(text, n=320):
    text = clean(text)
    return text[:n] + "…" if len(text) > n else text


def entry(title, synopsis, showtimes, theater, address, url="", rating="", runtime="", poster=""):
    # Deduplicate showtimes while preserving order
    seen = set()
    deduped = []
    for t in showtimes:
        t = clean(t)
        if t and t not in seen:
            seen.add(t)
            deduped.append(t)
    return {
        "title":     clean(title),
        "synopsis":  truncate(synopsis),
        "showtimes": deduped,
        "theater":   theater,
        "address":   address,
        "url":       url,
        "rating":    clean(rating),
        "runtime":   clean(runtime),
        "poster":    poster,
    }


def is_today(text):
    text = str(text)
    return any(s in text for s in [TODAY_ISO, TODAY_MDY, TODAY_MDY2, TODAY_DOW,
                                    TODAY_ABBREV, "Today", "today", "TODAY"])


def playwright_page(url, selector, wait_ms=20000, extra_sleep=1.5):
    """Launch a headless Playwright browser, navigate to url, wait for selector."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx  = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        try:
            page.wait_for_selector(selector, timeout=wait_ms)
        except Exception:
            pass
        time.sleep(extra_sleep)
        html = page.content()
        browser.close()
    return html


# ─── Shared Fandango scraper (AMC + Kendall both use this) ───────────────────

def scrape_fandango_theater(fandango_slug, name, address):
    """
    Scrape any theater whose page is at fandango.com/{fandango_slug}/theater-page.
    Uses Playwright to render the JS-heavy page, then parses the shared
    .shared-movie-showtimes structure that Fandango uses for all theaters.
    """
    url = f"https://www.fandango.com/{fandango_slug}/theater-page?date={TODAY_ISO}"
    try:
        html = playwright_page(url, ".shared-movie-showtimes", wait_ms=22000)
        soup = BeautifulSoup(html, "lxml")
        movies = []

        for li in soup.select(".shared-movie-showtimes"):
            article = li.select_one(".shared-movie-showtimes__movie")
            if not article:
                continue
            title_el = article.select_one(".shared-movie-showtimes__movie-title-link")
            if not title_el:
                continue
            title = clean(title_el.get_text())

            rating_el = article.select_one("data.shared-showtimes__movie-rating")
            rating    = clean(rating_el.get("value", "") or rating_el.get_text()) if rating_el else ""
            runtime_m = re.search(r"(\d[\d\s]*hr[\s\d]*min|\d+\s*min)", clean(article.get_text()))
            runtime   = runtime_m.group(1) if runtime_m else ""

            img_el    = article.select_one(".shared-movie-showtimes__movie-poster")
            poster    = img_el.get("src", "") if img_el else ""

            detail_el = article.select_one("a[href*='/movie-overview']")
            detail_url = ("https://www.fandango.com" + detail_el["href"]
                          if detail_el and detail_el["href"].startswith("/") else url)

            st_section = li.select_one(".shared-movie-showtimes__showtimes")
            raw_times  = []
            if st_section:
                for a in st_section.find_all("a"):
                    t = clean(a.get_text())
                    if re.match(r"\d{1,2}:\d{2}", t):
                        # "2:00p" → "2:00 PM"
                        t = re.sub(r"(\d{1,2}:\d{2})\s*([ap])\b",
                                   lambda m: m.group(1) + " " + m.group(2).upper() + "M", t)
                        raw_times.append(t)

            movies.append(entry(title, "", raw_times, name, address, detail_url, rating, runtime, poster))

        return movies or [{"error": f"{name}: no movies found on Fandango.", "theater": name, "address": address, "url": url}]

    except ImportError:
        return [{"error": "Playwright not installed. Run: pip3 install playwright && playwright install chromium",
                 "theater": name, "address": address}]
    except Exception as e:
        return [{"error": f"{name} (Fandango) scrape failed: {e}", "theater": name, "address": address, "url": url}]


def scrape_amc_boston_common():
    return scrape_fandango_theater(
        "amc-boston-common-19-aapnv",
        "AMC Boston Common 19",
        "175 Tremont St, Boston, MA",
    )


def scrape_kendall_square():
    return scrape_fandango_theater(
        "landmark-kendall-square-cinema-aaeis",
        "Kendall Square Cinema",
        "1 Kendall Sq, Cambridge, MA",
    )


# ─── Somerville Theatre (WordPress WP Theatre plugin) ─────────────────────────

def scrape_somerville_theatre():
    NAME = "Somerville Theatre"
    ADDR = "55 Davis Sq, Somerville, MA"
    BASE = "https://www.somervilletheatre.com"

    try:
        r    = get(BASE + "/movies/")
        soup = BeautifulSoup(r.text, "lxml")

        prod_urls = list(dict.fromkeys(
            a["href"] for a in soup.select("a[href]")
            if "/production/" in a.get("href", "")
        ))

        def fetch_production(purl):
            try:
                pr    = get(purl, timeout=12)
                psoup = BeautifulSoup(pr.text, "lxml")

                title_el = psoup.select_one("h1.entry-title, h1")
                title    = clean(title_el.get_text()) if title_el else (
                    purl.rstrip("/").split("/")[-1].replace("-", " ").title()
                )

                syn_el   = psoup.select_one(".wpt_production_description, .entry-content > p")
                synopsis = clean(syn_el.get_text()) if syn_el else ""

                img_el   = psoup.select_one("img.wp-post-image, img.attachment-poster")
                poster   = (img_el.get("data-src") or img_el.get("src", "")) if img_el else ""
                if poster.startswith("data:"):
                    poster = ""

                today_times = []
                for ev in psoup.select(".wp_theatre_event"):
                    date_el  = ev.select_one(".wp_theatre_event_startdate")
                    date_txt = clean(date_el.get_text()) if date_el else ""
                    if not is_today(date_txt):
                        continue
                    time_el = ev.select_one(".wp_theatre_event_starttime")
                    if time_el:
                        today_times.append(clean(time_el.get_text()))

                return entry(title, synopsis, today_times, NAME, ADDR, purl, poster=poster) if today_times else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(fetch_production, prod_urls))

        movies = [m for m in results if m]
        return movies or [{"error": "No Somerville Theatre screenings found for today.", "theater": NAME, "address": ADDR, "url": BASE}]

    except Exception as e:
        return [{"error": str(e), "theater": NAME, "address": ADDR}]


# ─── Capitol Theatre (Arlington, MA) ──────────────────────────────────────────

def scrape_capitol_theater():
    """
    Capitol Theatre – capitoltheatreusa.com
    Uses the same WordPress WP Theatre plugin as Somerville Theatre.
    Scrapes the movies listing page, then visits each individual movie page
    to find today's .wp_theatre_event showtimes.
    """
    NAME = "Capitol Theatre"
    ADDR = "204 Massachusetts Ave, Arlington, MA"
    BASE = "https://www.capitoltheatreusa.com"

    try:
        r    = get(BASE + "/movies/todays-schedule/")
        soup = BeautifulSoup(r.text, "lxml")

        prod_urls = list(dict.fromkeys(
            a["href"] for a in soup.select("a[href]")
            if "/movie/" in a.get("href", "")
        ))
        if not prod_urls:
            # Fall back to full movies listing
            r2   = get(BASE + "/movies/")
            soup2 = BeautifulSoup(r2.text, "lxml")
            prod_urls = list(dict.fromkeys(
                a["href"] for a in soup2.select("a[href]")
                if "/movie/" in a.get("href", "")
            ))

        def fetch_production(purl):
            try:
                pr    = get(purl, timeout=12)
                psoup = BeautifulSoup(pr.text, "lxml")

                title_el = psoup.select_one("h1.entry-title, h1")
                title    = clean(title_el.get_text()) if title_el else (
                    purl.rstrip("/").split("/")[-1].replace("-", " ").title()
                )
                syn_el   = psoup.select_one(".wpt_production_description, .entry-content > p")
                synopsis = clean(syn_el.get_text()) if syn_el else ""
                img_el   = psoup.select_one("img.wp-post-image, img.attachment-poster")
                poster   = (img_el.get("data-src") or img_el.get("src", "")) if img_el else ""
                if poster.startswith("data:"):
                    poster = ""

                today_times = []
                for ev in psoup.select(".wp_theatre_event"):
                    date_el  = ev.select_one(".wp_theatre_event_startdate")
                    date_txt = clean(date_el.get_text()) if date_el else ""
                    if not is_today(date_txt):
                        continue
                    time_el = ev.select_one(".wp_theatre_event_starttime")
                    if time_el:
                        today_times.append(clean(time_el.get_text()))

                return entry(title, synopsis, today_times, NAME, ADDR, purl, poster=poster) if today_times else None
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(fetch_production, prod_urls))

        movies = [m for m in results if m]
        return movies or [{"error": "No Capitol Theatre screenings found for today.", "theater": NAME, "address": ADDR, "url": BASE}]

    except Exception as e:
        return [{"error": str(e), "theater": NAME, "address": ADDR}]


# ─── Brattle Theatre ──────────────────────────────────────────────────────────

def scrape_brattle_theatre():
    """
    Brattle homepage lists current films as .show divs.
    Each film's page has .showtimes-description > .single-show-showtimes with
    ol.showtimes > li > span.showtime elements containing time + "Sold Out" etc.
    """
    NAME = "Brattle Theatre"
    ADDR = "40 Brattle St, Cambridge, MA"
    BASE = "https://www.brattlefilm.org"

    try:
        r    = get(BASE)
        soup = BeautifulSoup(r.text, "lxml")

        film_data = []
        for show in soup.select(".show"):
            link_el = show.select_one("a[href]")
            title_h = show.select_one("h2")
            desc_el = show.select_one(".show__description, .show__subtitle")
            style   = show.get("style", "")
            poster_m = re.search(r"url\(([^)]+)\)", style)
            poster   = poster_m.group(1).strip("'\"") if poster_m else ""
            if link_el and title_h:
                href = link_el["href"]
                film_data.append({
                    "title":    clean(title_h.get_text()),
                    "url":      href if href.startswith("http") else BASE + href,
                    "synopsis": clean(desc_el.get_text()) if desc_el else "",
                    "poster":   poster,
                })

        def fetch_showtimes(film):
            try:
                pr    = get(film["url"], timeout=12)
                psoup = BeautifulSoup(pr.text, "lxml")

                st_desc = psoup.select_one(".showtimes-description")
                if not st_desc:
                    return None

                today_times = []

                # Check if today's date is active / selected in the date tabs
                all_date_tabs = st_desc.select(".show-datelist, .show-date, [class*='date']")
                today_tab_active = any(is_today(t.get_text()) for t in all_date_tabs)

                if today_tab_active or is_today(pr.text):
                    # Get time spans from the showtimes ordered list
                    for span in st_desc.select("ol.showtimes span.showtime"):
                        raw = clean(span.get_text())
                        if re.search(r"\d{1,2}:\d{2}", raw):
                            today_times.append(raw)

                if not today_times:
                    return None

                # Richer synopsis from the film page
                syn_el = psoup.select_one(".show-content p, .show-description p, .entry-content p")
                synopsis = clean(syn_el.get_text()) if syn_el else film["synopsis"]

                runtime_m = re.search(r"Run Time:\s*([\d]+ min\.?)", pr.text)
                runtime   = runtime_m.group(1).rstrip(".") if runtime_m else ""
                dir_m     = re.search(r"Director:\s*([^\n<|]+)", pr.text)
                director  = clean(dir_m.group(1)) if dir_m else ""
                if director:
                    synopsis = f"Dir. {director}. " + synopsis

                return entry(film["title"], synopsis, today_times, NAME, ADDR, film["url"],
                             runtime=runtime, poster=film["poster"])
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(fetch_showtimes, film_data))

        movies = [m for m in results if m]
        return movies or [{"error": "No Brattle screenings today.", "theater": NAME, "address": ADDR, "url": BASE}]

    except Exception as e:
        return [{"error": str(e), "theater": NAME, "address": ADDR}]


# ─── Harvard Film Archive ─────────────────────────────────────────────────────

def scrape_harvard_film_archive():
    """
    harvardfilmarchive.org – each .event has a datetime attr like '2026-03-20 19:00',
    .event__title, .event__time, and .event__series.
    Individual event pages have the synopsis.
    """
    NAME = "Harvard Film Archive"
    ADDR = "24 Quincy St (Carpenter Center), Cambridge, MA"
    BASE = "https://harvardfilmarchive.org"

    try:
        r    = get(BASE, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        today_events = []

        for ev in soup.select(".event"):
            dt_els = ev.find_all(attrs={"datetime": True})
            if not dt_els:
                continue
            dt_val = dt_els[0]["datetime"]
            if not str(dt_val).startswith(TODAY_ISO):
                continue

            title_el  = ev.select_one(".event__title")
            time_el   = ev.select_one(".event__time")
            series_el = ev.select_one(".event__series")
            link_el   = ev.select_one(".event__link[href]")
            img_el    = ev.select_one("img")

            title   = clean(title_el.get_text())  if title_el  else ""
            showtime = clean(time_el.get_text())   if time_el   else ""
            series  = clean(series_el.get_text()) if series_el else ""
            link    = link_el["href"]              if link_el   else BASE
            if not link.startswith("http"):
                link = BASE + link
            poster  = (img_el.get("src","") if img_el else "")
            if poster and not poster.startswith("http"):
                poster = BASE + poster

            if title:
                today_events.append({
                    "title": title, "time": showtime,
                    "series": series, "link": link, "poster": poster,
                })

        def fetch_synopsis(ev_data):
            synopsis = ev_data["series"]
            poster   = ev_data["poster"]
            try:
                ep   = get(ev_data["link"], timeout=10)
                esoup = BeautifulSoup(ep.text, "lxml")
                body  = esoup.select_one(
                    ".field-body, .field--type-text-with-summary, "
                    ".field--name-body, article > .content p, article p"
                )
                if body:
                    synopsis = truncate(clean(body.get_text()))
                if not poster:
                    img2 = esoup.select_one("article img, .field--type-image img")
                    if img2:
                        src = img2.get("src", "")
                        poster = BASE + src if src and not src.startswith("http") else src
            except Exception:
                pass
            return entry(
                ev_data["title"], synopsis, [ev_data["time"]] if ev_data["time"] else [],
                NAME, ADDR, ev_data["link"], poster=poster,
            )

        with ThreadPoolExecutor(max_workers=6) as ex:
            movies = list(ex.map(fetch_synopsis, today_events))

        return movies or [{"error": "No HFA screenings today.", "theater": NAME, "address": ADDR, "url": BASE}]

    except Exception as e:
        return [{"error": str(e), "theater": NAME, "address": ADDR}]


# ─── Coolidge Corner Theatre ──────────────────────────────────────────────────

def scrape_coolidge_corner():
    """
    Coolidge Corner Theatre – coolidge.org
    The /showtimes?date=YYYY-MM-DD page lists all films for a given day.
    Each film is a .film-card with title, runtime, synopsis, poster,
    and .showtime-ticket__time buttons.
    """
    NAME = "Coolidge Corner Theatre"
    ADDR = "290 Harvard St, Brookline, MA"
    BASE = "https://www.coolidge.org"

    try:
        url  = f"{BASE}/showtimes?date={TODAY_ISO}"
        r    = get(url, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        movies = []
        for card in soup.select("div.film-card"):
            # Title + detail link
            title_el = card.select_one(".film-card__title h2 a.film-card__link")
            if not title_el:
                continue
            title = clean(title_el.get_text())
            href  = title_el.get("href", "")
            detail_url = href if href.startswith("http") else BASE + href

            # Runtime
            rt_el   = card.select_one(".film-card__runtime")
            runtime = clean(rt_el.get_text()) if rt_el else ""

            # Synopsis
            syn_el   = card.select_one(".film-card__excerpt p")
            synopsis = clean(syn_el.get_text()) if syn_el else ""

            # Poster
            img_el = card.select_one(".film-card__image img")
            poster = ""
            if img_el:
                poster = img_el.get("src", "")
                if poster and not poster.startswith("http"):
                    poster = BASE + poster

            # Showtimes
            times = []
            for st in card.select(".showtime-ticket__time"):
                t = clean(st.get_text())
                if t:
                    times.append(t)

            if times:
                movies.append(entry(title, synopsis, times, NAME, ADDR,
                                    detail_url, runtime=runtime, poster=poster))

        return movies or [{"error": "No Coolidge Corner screenings found for today.",
                           "theater": NAME, "address": ADDR, "url": url}]

    except Exception as e:
        return [{"error": str(e), "theater": NAME, "address": ADDR}]


# ─── Classical Scene – Boston area classical music ───────────────────────────

def scrape_classical_scene():
    """
    classical-scene.com/calendar/ lists all upcoming classical music events.
    Each event is a div.event0 or div.event-1 with date, time, presenter,
    performer, venue, and programme notes.  We filter for today's date.
    """
    NAME = "Classical Music (Boston)"
    ADDR = "classical-scene.com"
    URL  = "https://classical-scene.com/calendar/"

    try:
        r    = get(URL, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        # Build the date string the page uses, e.g. "Friday, March 20, 2026"
        target_date = f"{TODAY_DOW}, {TODAY_MDY}, {datetime.strptime(TODAY_ISO, '%Y-%m-%d').year}"

        events = []
        for ev in soup.select("div.event0, div.event-1"):
            date_el = ev.select_one("span.date")
            if not date_el or clean(date_el.get_text()) != target_date:
                continue

            time_el = ev.select_one("span.time")
            ev_time = clean(time_el.get_text()) if time_el else ""

            city_el = ev.select_one("span.city")
            city    = clean(city_el.get_text()) if city_el else ""

            ul = ev.select_one("ul.right-c")
            if not ul:
                continue

            # Presenter (organisation)
            pres_el = ul.select_one("li.presenter")
            presenter = ""
            if pres_el:
                pres_a = pres_el.select_one("a")
                presenter = clean(pres_a.get_text()) if pres_a else clean(pres_el.get_text())
                presenter = re.sub(r"\s*presents\s*$", "", presenter)

            # Performer / title line
            perf_el = ul.select_one("li.performer")
            performer = clean(perf_el.get_text()) if perf_el else ""

            # Venue
            venue_a = ul.select_one("a.gigpress-address")
            venue   = clean(venue_a.get_text()) if venue_a else ""

            # Ticket link
            ticket_url = ""
            for li in ul.find_all("li"):
                if "Tickets:" in li.get_text():
                    t_a = li.select_one("a")
                    if t_a:
                        ticket_url = t_a.get("href", "")
                    break

            # Programme notes (the <li> after li.notes, or last <li> with <p>)
            notes = ""
            notes_li = ul.select_one("li.notes")
            if notes_li:
                next_li = notes_li.find_next_sibling("li")
                if next_li:
                    notes = clean(next_li.get_text())
            if not notes:
                # fallback: last li with a <p>
                for li in reversed(ul.find_all("li")):
                    if li.select_one("p"):
                        notes = clean(li.get_text())
                        break

            # Build a title from presenter + performer
            title = presenter
            if performer:
                title = f"{presenter} – {performer}" if presenter else performer

            synopsis = notes
            address  = f"{venue}, {city}" if venue and city else (venue or city)

            events.append(entry(
                title, synopsis, [ev_time] if ev_time else [],
                NAME, address, ticket_url or URL,
            ))

        return events or [{"error": "No classical music events found for today.",
                           "theater": NAME, "address": ADDR, "url": URL}]

    except Exception as e:
        return [{"error": str(e), "theater": NAME, "address": ADDR}]


# ─── Boston Ballet ────────────────────────────────────────────────────────────

def scrape_boston_ballet():
    """
    bostonballet.org – fetch the performances listing page, visit each
    production page, and extract showtimes from JSON-LD Event objects.
    """
    NAME = "Boston Ballet"
    ADDR = "Citizens Bank Opera House, 539 Washington St, Boston, MA"
    BASE = "https://www.bostonballet.org"

    try:
        r    = get(BASE + "/home/tickets-performances/", timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        # Collect performance detail URLs
        perf_urls = []
        for a in soup.select("a[href*='/performances/']"):
            href = a["href"]
            full = href if href.startswith("http") else BASE + href
            if full not in perf_urls:
                perf_urls.append(full)

        def fetch_performance(purl):
            try:
                pr   = get(purl, timeout=12)
                psoup = BeautifulSoup(pr.text, "lxml")

                events = []
                for script in psoup.select('script[type="application/ld+json"]'):
                    try:
                        data = json.loads(script.string)
                    except Exception:
                        continue
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if item.get("@type") != "Event":
                            continue
                        start = item.get("startDate", "")
                        if isinstance(start, list):
                            start = start[0] if start else ""
                        if not isinstance(start, str) or not start.startswith(TODAY_ISO):
                            continue
                        # Parse time
                        try:
                            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                            t = dt.strftime("%-I:%M %p")
                        except Exception:
                            t = start[11:16] if len(start) > 16 else ""

                        title   = item.get("name", "")
                        desc    = item.get("description", "")
                        img     = item.get("image", "")
                        loc     = item.get("location", {})
                        venue   = loc.get("name", "") if isinstance(loc, dict) else ""
                        address = ""
                        if isinstance(loc, dict) and isinstance(loc.get("address"), dict):
                            a = loc["address"]
                            address = f"{a.get('streetAddress','')}, {a.get('addressLocality','')}, {a.get('addressRegion','')}"
                        ticket_url = ""
                        offers = item.get("offers", {})
                        if isinstance(offers, dict):
                            ticket_url = offers.get("url", "")
                        elif isinstance(offers, list) and offers:
                            ticket_url = offers[0].get("url", "")

                        events.append({
                            "title": title, "time": t,
                            "synopsis": desc, "poster": img,
                            "venue": venue, "address": address or ADDR,
                            "url": ticket_url or purl,
                        })
                return events
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=6) as ex:
            all_events = []
            for result in ex.map(fetch_performance, perf_urls[:15]):
                all_events.extend(result)

        # Group showtimes by title
        by_title = {}
        for ev in all_events:
            key = ev["title"]
            if key not in by_title:
                by_title[key] = ev
                by_title[key]["times"] = []
            by_title[key]["times"].append(ev["time"])

        movies = []
        for show in by_title.values():
            movies.append(entry(
                show["title"], show["synopsis"], show["times"],
                NAME, show["address"], show["url"],
                poster=show["poster"],
            ))

        return movies or [{"error": "No Boston Ballet performances today.",
                           "theater": NAME, "address": ADDR,
                           "url": BASE + "/home/tickets-performances/"}]

    except Exception as e:
        return [{"error": str(e), "theater": NAME, "address": ADDR}]


# ─── boston-theater.com dance listings ────────────────────────────────────────

def scrape_boston_theater_dance():
    """
    boston-theater.com/shows/dance/ – Nuxt.js site with a _payload.json
    endpoint that returns all dance/ballet shows as structured data.
    """
    NAME = "Dance & Ballet (Boston)"
    ADDR = "boston-theater.com"
    URL  = "https://www.boston-theater.com/shows/dance/_payload.json"

    try:
        r = get(URL, timeout=15)
        # The payload is a Nuxt-specific format; try to parse it
        text = r.text

        # Extract show data from the payload — look for event objects
        # The payload uses a compact format; we'll fall back to scraping HTML
        # if the JSON parse doesn't work cleanly
        shows = []

        try:
            data = json.loads(text)
            # Navigate the Nuxt payload structure
            # The structure varies but typically has show arrays
            if isinstance(data, dict):
                # Look for arrays that contain event-like objects
                def find_events(obj, depth=0):
                    if depth > 5:
                        return
                    if isinstance(obj, dict):
                        if "eventName" in obj or "eventSlug" in obj:
                            shows.append(obj)
                        else:
                            for v in obj.values():
                                find_events(v, depth + 1)
                    elif isinstance(obj, list):
                        for item in obj:
                            find_events(item, depth + 1)
                find_events(data)
        except json.JSONDecodeError:
            pass

        # If payload parsing worked, filter for today's shows
        if shows:
            events = []
            for show in shows:
                # Check dates
                start = show.get("startDate", show.get("eventStartDate", ""))
                end   = show.get("endDate", show.get("eventEndDate", ""))
                name  = show.get("eventName", show.get("name", ""))
                if not name:
                    continue
                # Check if today falls within the show's run
                if start and end:
                    if not (start[:10] <= TODAY_ISO <= end[:10]):
                        continue
                elif start and not start[:10] <= TODAY_ISO:
                    continue

                venue_name = show.get("venueName", "")
                venue_addr = ""
                for k in ("venueAddress", "venueCity", "venueState"):
                    if show.get(k):
                        venue_addr += show[k] + " "
                desc    = show.get("eventDescription", show.get("description", ""))
                img     = show.get("eventImage", show.get("image", ""))
                slug    = show.get("eventSlug", "")
                ev_url  = f"https://www.boston-theater.com/shows/{slug}" if slug else ""

                events.append(entry(
                    name, clean(desc), [],
                    NAME, f"{venue_name}, {venue_addr}".strip(", ") if venue_name else ADDR,
                    ev_url, poster=img,
                ))
            if events:
                return events

        # Fallback: scrape the HTML page for dance listings
        html_url = "https://www.boston-theater.com/shows/dance/"
        r2   = get(html_url, timeout=15)
        soup = BeautifulSoup(r2.text, "lxml")

        events = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string)
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") not in ("Event", "DanceEvent", "TheaterEvent"):
                    continue
                start = item.get("startDate", "")
                end   = item.get("endDate", start)
                if start and end and not (start[:10] <= TODAY_ISO <= end[:10]):
                    continue
                title = item.get("name", "")
                if not title:
                    continue
                desc  = item.get("description", "")
                img   = item.get("image", "")
                loc   = item.get("location", {})
                venue = loc.get("name", "") if isinstance(loc, dict) else ""
                url   = item.get("url", "")
                events.append(entry(
                    title, clean(desc), [], NAME,
                    venue or ADDR, url, poster=img,
                ))

        # Deduplicate by title (boston-theater may overlap with Boston Ballet)
        seen = set()
        deduped = []
        for e in events:
            if e["title"] not in seen:
                seen.add(e["title"])
                deduped.append(e)

        return deduped or [{"error": "No dance events found for today.",
                            "theater": NAME, "address": ADDR,
                            "url": "https://www.boston-theater.com/shows/dance/"}]

    except Exception as e:
        return [{"error": str(e), "theater": NAME, "address": ADDR}]


# ─── Registry & Routes ────────────────────────────────────────────────────────

SCRAPERS = [
    ("AMC Boston Common 19",      scrape_amc_boston_common),
    ("Kendall Square Cinema",     scrape_kendall_square),
    ("Somerville Theatre",        scrape_somerville_theatre),
    ("Capitol Theatre",           scrape_capitol_theater),
    ("Coolidge Corner Theatre",   scrape_coolidge_corner),
    ("Brattle Theatre",           scrape_brattle_theatre),
    ("Harvard Film Archive",      scrape_harvard_film_archive),
    ("Classical Music (Boston)",  scrape_classical_scene),
    ("Boston Ballet",             scrape_boston_ballet),
    ("Dance & Ballet (Boston)",   scrape_boston_theater_dance),
]


@app.route("/api/showtimes")
def api_showtimes():
    date_param = request.args.get("date")
    _refresh_today(date_param)
    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        future_to_name = {ex.submit(fn): name for name, fn in SCRAPERS}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                results[name] = future.result(timeout=70)
            except Exception as e:
                results[name] = [{"error": str(e), "theater": name}]
    return jsonify({name: results.get(name, []) for name, _ in SCRAPERS})


# ─── UI ───────────────────────────────────────────────────────────────────────

CLASSICAL_KEY = "Classical Music (Boston)"
DANCE_KEYS = {"Boston Ballet", "Dance & Ballet (Boston)"}

MOVIE_PILLS = {
    'amc': 'AMC Boston Common 19', 'kendall': 'Kendall Square Cinema',
    'somerville': 'Somerville Theatre', 'capitol': 'Capitol Theatre',
    'coolidge': 'Coolidge Corner Theatre', 'brattle': 'Brattle Theatre',
    'hfa': 'Harvard Film Archive',
}

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <title>What's Up Today</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    :root{color-scheme:dark}
    body{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text',Helvetica,sans-serif}
    .chip{display:inline-block;background:#1e293b;color:#93c5fd;border:1px solid #2563eb44;border-radius:20px;padding:3px 11px;font-size:.73rem;font-variant-numeric:tabular-nums;margin:2px 3px 0 0;white-space:nowrap;font-weight:600}
    .pill{display:inline-flex;align-items:center;padding:4px 13px;border-radius:99px;font-size:.72rem;font-weight:600;cursor:pointer;white-space:nowrap;transition:all .15s;user-select:none}
    .pill.active{background:#f59e0b;color:#1c1917}
    .pill.inactive{background:#1e293b;color:#94a3b8}
    .tab{padding:7px 18px;border-radius:10px 10px 0 0;font-size:.82rem;font-weight:700;cursor:pointer;transition:all .15s;user-select:none;border:1px solid transparent;border-bottom:none}
    .tab.active{background:#0f172a;color:#f59e0b;border-color:#1e293b}
    .tab.inactive{background:#020617;color:#64748b}
    .spinner{border:3px solid rgba(255,255,255,.12);border-top-color:#f59e0b;border-radius:50%;width:34px;height:34px;animation:spin .8s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}
    .card{background:#0f172a;border:1px solid #1e293b;border-radius:16px;overflow:hidden;margin-bottom:20px}
    .card-hdr{background:linear-gradient(135deg,#0f1f3b 0%,#1e3a5f 100%);padding:13px 16px}
    .card-hdr-music{background:linear-gradient(135deg,#1f0f3b 0%,#3b1e5f 100%);padding:13px 16px}
    .card-hdr-dance{background:linear-gradient(135deg,#2d0f1f 0%,#5f1e3b 100%);padding:13px 16px}
    .movie-row{padding:14px 16px;border-top:1px solid #1e293b;display:flex;gap:12px}
    .poster{width:58px;height:84px;object-fit:cover;border-radius:7px;flex-shrink:0;background:#1e293b}
    .badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:.64rem;font-weight:700;background:#1e293b;color:#64748b;margin-right:4px}
    .more-btn{color:#f59e0b;font-size:.75rem;cursor:pointer;margin-left:2px}
    .date-input{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:3px 8px;font-size:.75rem;font-family:inherit;cursor:pointer}
    .date-input::-webkit-calendar-picker-indicator{filter:invert(0.7)}
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">

<div class="sticky top-0 z-50 bg-slate-950/95 backdrop-blur border-b border-slate-800">
  <div class="max-w-xl mx-auto px-4 pt-4 pb-2">
    <div class="flex items-start justify-between">
      <div>
        <h1 class="text-lg font-bold tracking-tight leading-tight">What's Up Today</h1>
        <div class="flex items-center gap-2 mt-1">
          <input type="date" id="date-picker" class="date-input" onchange="changeDate(this.value)">
          <button onclick="jumpDate(-1)" class="text-slate-400 text-xs px-1.5 py-0.5 rounded border border-slate-700 hover:text-white">&larr;</button>
          <button onclick="jumpDate(0)" class="text-slate-400 text-xs px-1.5 py-0.5 rounded border border-slate-700 hover:text-white">Today</button>
          <button onclick="jumpDate(1)" class="text-slate-400 text-xs px-1.5 py-0.5 rounded border border-slate-700 hover:text-white">&rarr;</button>
        </div>
      </div>
      <button onclick="reload()" class="text-amber-400 text-xs px-3 py-1.5 rounded-full border border-amber-400/30 mt-0.5 shrink-0">Refresh</button>
    </div>
  </div>
  <!-- Tabs -->
  <div class="max-w-xl mx-auto px-4 pt-3 flex gap-1">
    <span class="tab active"   id="tab-classical" onclick="setTab('classical')">Classical</span>
    <span class="tab inactive" id="tab-dance"     onclick="setTab('dance')">Ballet & Dance</span>
    <span class="tab inactive" id="tab-movies"    onclick="setTab('movies')">Movies</span>
  </div>
  <!-- Movie sub-filters (hidden when classical tab active) -->
  <div id="movie-filters" class="max-w-xl mx-auto px-4 py-2 flex gap-2 overflow-x-auto border-t border-slate-800" style="display:none;scrollbar-width:none">
    <span class="pill active"   id="f-all"        onclick="setFilter('all')">All Theaters</span>
    <span class="pill inactive" id="f-amc"        onclick="setFilter('amc')">AMC</span>
    <span class="pill inactive" id="f-kendall"    onclick="setFilter('kendall')">Kendall</span>
    <span class="pill inactive" id="f-somerville" onclick="setFilter('somerville')">Somerville</span>
    <span class="pill inactive" id="f-capitol"    onclick="setFilter('capitol')">Capitol</span>
    <span class="pill inactive" id="f-coolidge"   onclick="setFilter('coolidge')">Coolidge</span>
    <span class="pill inactive" id="f-brattle"    onclick="setFilter('brattle')">Brattle</span>
    <span class="pill inactive" id="f-hfa"        onclick="setFilter('hfa')">HFA</span>
  </div>
</div>

<div id="root" class="max-w-xl mx-auto px-4 pt-4 pb-10"></div>

<script>
const CLASSICAL_KEY='Classical Music (Boston)';
const DANCE_KEYS=['Boston Ballet','Dance & Ballet (Boston)'];
const MOVIE_PILLS={all:'',amc:'AMC Boston Common 19',kendall:'Kendall Square Cinema',
  somerville:'Somerville Theatre',capitol:'Capitol Theatre',coolidge:'Coolidge Corner Theatre',
  brattle:'Brattle Theatre',hfa:'Harvard Film Archive'};
const ALL_TABS=['classical','dance','movies'];
let DATA={},currentTab='classical',movieFilter='all';

// Date helpers
function todayISO(){return new Date().toISOString().slice(0,10)}
let selectedDate=todayISO();
const dp=document.getElementById('date-picker');
dp.value=selectedDate;

function changeDate(iso){
  selectedDate=iso;
  dp.value=iso;
  reload();
}
function jumpDate(offset){
  if(offset===0){changeDate(todayISO());return}
  const d=new Date(selectedDate+'T12:00:00');
  d.setDate(d.getDate()+offset);
  changeDate(d.toISOString().slice(0,10));
}

function setTab(tab){
  currentTab=tab;
  ALL_TABS.forEach(t=>{
    document.getElementById('tab-'+t).className='tab '+(t===tab?'active':'inactive');
  });
  document.getElementById('movie-filters').style.display=tab==='movies'?'flex':'none';
  render();
}

function setFilter(key){
  movieFilter=key;
  Object.keys(MOVIE_PILLS).forEach(k=>{
    document.getElementById('f-'+k).className='pill '+(k===key?'active':'inactive');
  });
  render();
}

const LOADING=`<div class="flex flex-col items-center py-24 gap-4">
  <div class="spinner"></div>
  <p class="text-slate-400 text-sm">Fetching from 10 sources…</p>
  <p class="text-slate-600 text-xs">This takes about 30–50 seconds</p>
</div>`;

async function reload(){
  document.getElementById('root').innerHTML=LOADING;
  try{
    const r=await fetch('/api/showtimes?date='+selectedDate);
    DATA=await r.json();
    render();
  }catch(e){
    document.getElementById('root').innerHTML=
      `<p class="text-red-400 text-center py-16">Failed to connect.<br><small class="text-slate-600">${e}</small></p>`;
  }
}

function renderCard(theater, movies, cardType){
  // cardType: 'movie', 'music', 'dance'
  const valid=movies.filter(m=>m.title&&!m.error);
  const err=movies.find(m=>m.error);
  const hdrClass=cardType==='dance'?'card-hdr-dance':cardType==='music'?'card-hdr-music':'card-hdr';
  const icon=cardType==='dance'?'🩰':cardType==='music'?'🎵':'🎞';
  let html=`<div class="card">
    <div class="${hdrClass}">
      <div class="flex items-start justify-between">
        <div>
          <h2 class="font-bold text-white text-base leading-snug">${esc(theater)}</h2>
          <p class="text-slate-400 text-xs mt-0.5">${esc(movies[0]?.address||'')}</p>
        </div>
        <span class="text-slate-500 text-xs mt-0.5 shrink-0 ml-2">${valid.length} event${valid.length!==1?'s':''}</span>
      </div>
    </div>`;
  if(err&&!valid.length){
    const link=err.url?`<a href="${esc(err.url)}" target="_blank" rel="noopener" class="text-amber-400 underline text-xs ml-1">Open →</a>`:'';
    html+=`<div class="px-4 py-3 text-amber-400 text-sm leading-relaxed">${esc(err.error)}${link}</div>`;
  }else if(!valid.length){
    html+=`<div class="px-4 py-3 text-slate-500 text-sm italic">Nothing scheduled.</div>`;
  }else{
    for(const m of valid){
      const id='x'+Math.random().toString(36).slice(2);
      const longSyn=m.synopsis&&m.synopsis.length>130;
      const shortSyn=longSyn?m.synopsis.slice(0,130)+'…':m.synopsis;
      html+=`<div class="movie-row">
        ${m.poster?`<img class="poster" src="${esc(m.poster)}" loading="lazy" onerror="this.style.display='none'" alt="">`:
          `<div class="poster flex items-center justify-center text-2xl">${icon}</div>`}
        <div class="flex-1 min-w-0">
          <h3 class="font-semibold text-white leading-snug">${esc(m.title)}</h3>
          <div class="mt-0.5 flex flex-wrap gap-0.5">
            ${m.rating?`<span class="badge">${esc(m.rating)}</span>`:''}
            ${m.runtime?`<span class="badge">${esc(m.runtime)}</span>`:''}
          </div>
          ${m.synopsis?`<p class="text-slate-400 text-sm mt-1.5 leading-relaxed">
            <span id="${id}s">${esc(shortSyn)}</span>
            <span id="${id}f" style="display:none">${esc(m.synopsis)}</span>
            ${longSyn?`<span class="more-btn" onclick="expand('${id}',this)">more</span>`:''}
          </p>`:''}
          ${m.showtimes?.length
            ?`<div class="mt-2">${m.showtimes.map(t=>`<span class="chip">${esc(t)}</span>`).join('')}</div>`
            :`<p class="text-slate-600 text-xs mt-2 italic">No times listed</p>`}
          ${m.url?`<a href="${esc(m.url)}" target="_blank" rel="noopener" class="text-amber-400 text-xs mt-2 inline-block hover:underline">Tickets / Details →</a>`:''}
        </div>
      </div>`;
    }
  }
  html+='</div>';
  return html;
}

function render(){
  let html='';
  if(currentTab==='classical'){
    const cm=DATA[CLASSICAL_KEY];
    if(cm) html+=renderCard(CLASSICAL_KEY,cm,'music');
    else html='<p class="text-slate-500 text-center py-16">No classical music data.</p>';
  }else if(currentTab==='dance'){
    for(const key of DANCE_KEYS){
      const d=DATA[key];
      if(d) html+=renderCard(key,d,'dance');
    }
    if(!html) html='<p class="text-slate-500 text-center py-16">No dance data.</p>';
  }else{
    for(const [theater,movies] of Object.entries(DATA)){
      if(theater===CLASSICAL_KEY||DANCE_KEYS.includes(theater)) continue;
      if(movieFilter!=='all'&&MOVIE_PILLS[movieFilter]!==theater) continue;
      html+=renderCard(theater,movies,'movie');
    }
  }
  document.getElementById('root').innerHTML=html||'<p class="text-slate-500 text-center py-16">No results.</p>';
}

function expand(id,btn){
  document.getElementById(id+'s').style.display='none';
  document.getElementById(id+'f').style.display='inline';
  btn.style.display='none';
}
function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

reload();
</script>
</body>
</html>"""


def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.route("/")
def index():
    return render_template_string(HTML)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    local_ip = _get_local_ip()
    url = f"http://{local_ip}:8080"
    print()
    print("  What's Up Today – Boston Area")
    print("  ─────────────────────────────────────────────")
    print(f"  Mac:     http://localhost:8080")
    print(f"  Share:   {url}")
    print()
    print("  First load: ~30–50s  |  Ctrl+C to stop")
    print()
    app.run(host="0.0.0.0", port=8080, debug=False)
