"""
Google Business Profile.

Three modules, split by what they know about:

    oauth.py       consent, code exchange, and keeping an access token alive
    client.py      Google's APIs, and only Google's APIs
    connection.py  what a hotel's connection is, in our own terms

Nothing above `connection.py` knows that reviews come from a different API
version than locations do, and nothing below it knows what a property is.
"""
