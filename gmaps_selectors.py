"""Centralized CSS/XPath selectors for Google Maps scraping.

All selectors are defined here for easy maintenance when Google updates their DOM.
Based on patterns from revcli, SherlockMaps, and other scrapers.
"""

from __future__ import annotations


class SearchSelectors:
    """Selectors for Google Maps search results page."""

    SEARCH_INPUT = 'input#searchboxinput, input[name="search"]'
    SEARCH_BUTTON = 'button#searchbox-searchbutton, button[aria-label="Search"]'
    RESULTS_CONTAINER = 'div[role="feed"]'
    RESULT_ITEM = 'div[data-result-index], div.Nv2PK'
    RESULT_LINK = 'a.hfpxzc'
    RESULT_NAME = '.qBF1Pd, .fontHeadlineSmall'
    RESULT_RATING = '.MW4etd, span.ZkP5Je'
    RESULT_REVIEW_COUNT = '.UY7F9, span.ZkP5Je span[aria-label]'
    RESULT_ADDRESS = '.W4Efsd .W4Efsd span:last-child, .fontBodyMedium span.DkEaL'
    SHOW_MORE_BUTTON = 'button#searchbox-searchbutton'
    RESULTS_LOADING = 'div.m6QErb.DxyBCb.kA9KIf.dS8AEf'


class ReviewSelectors:
    """Selectors for Google Maps review panel."""

    REVIEWS_TAB = 'button[jsaction*="reviews"]'
    REVIEWS_LINK = 'div/font/span[aria-label*="review"]'
    REVIEWS_COUNT_LINK = 'button[aria-label*="review"]'
    SORT_DROPDOWN = 'div.Mjt6Ue select, div[role="listbox"]'
    SORT_OPTION_NEWEST = 'menuitem[data-value="1"]'
    REVIEW_CONTAINER = 'div.jftiEf, div[data-review-id]'
    REVIEW_AUTHOR = 'div.d4r55, .WNxzHc a, .rsqaWe'
    REVIEW_STARS = 'span.kvMYJc, span[role="img"][aria-label*="star"]'
    REVIEW_TEXT = 'span.wiI7pd, .wiI7pd, span[class*="review-text"]'
    REVIEW_DATE = 'span.rsqaWe, .rsqaWe'
    REVIEW_ORIGINAL_TEXT = 'span.lmb32e, button span.wiI7pd'
    REVIEW_SCROLLABLE = 'div.m6QErb.DxyBCb.kA9KIf.dS8AEf[tabindex="0"]'
    REVIEWS_PANE = 'div.m6QErb.DxyBCb'


class PlaceSelectors:
    """Selectors for individual Google Maps place page."""

    PLACE_NAME = 'h1.DUwDvf, h1[class*="header"]'
    PLACE_RATING = 'div.F7nice span[aria-hidden="true"]'
    PLACE_REVIEW_COUNT = 'div.F7nice span span[aria-label]'
    PLACE_ADDRESS = (
        'div.Io6YTe button[data-item-id="address"] div.fontBodyMedium'
    )
    PLACE_PHONE = (
        'div.Io6YTe button[data-item-id*="phone"] div.fontBodyMedium'
    )
    PLACE_WEBSITE = 'div.Io6YTe a[data-item-id="authority"]'
    PLACE_HOURS = (
        'div.t39EBf div[aria-label*="open"], div[aria-label*="hours"]'
    )
    PLACE_CATEGORY = 'button.DkEaL'
    AUTH_WALL = 'div.LPdwQe, div[class*="consent"]'
    COOKIE_CONSENT = 'button#L2AGLb, button[aria-label*="Accept"]'
    LIMITED_VIEW = 'div.LPdwQe'


class PhotoSelectors:
    """Selectors for Google Maps photo gallery."""

    # Tab on the place page to open the photo gallery
    PHOTOS_TAB = 'button[role="tab"][aria-label*="Photos"]'
    PHOTOS_TAB_ALT = 'button[aria-label*="photo" i], button[jsaction*="photos"]'
    # The main hero photo/button on the place page (click to open gallery)
    PHOTO_HERO = 'button[jsaction*="heroHeaderImage"], div.ZKCDEc'
    # Images within the photo gallery or place page
    PHOTO_IMG = 'img[src*="googleusercontent"]'
    PHOTO_IMG_BG = 'div[style*="googleusercontent"]'
    # Scrollable container in the photo gallery view
    PHOTO_GALLERY_SCROLL = 'div.m6QErb.DxyBCb'


class NavigationSelectors:
    """Selectors for navigation and page state."""

    COOKIE_ACCEPT_BUTTON = 'button#L2AGLb, button[aria-label="Accept all"]'
    COOKIE_ACCEPT_ITALIAN = (
        'button:has-text("Accetta tutto"), button:has-text("Accetta")'
    )
    LOADING_SPINNER = 'div.m6QErb.DxyBCb.l39Ib'
    PAGE_LOADED = 'div.m6QErb.DxyBCb'
    SIGN_IN_PROMPT = 'div.LPdwQe'
    DISMISS_SIGN_IN = 'button[aria-label="No thanks"]'