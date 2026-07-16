"""
Seed/refresh `group_trips` rows (idempotent upsert on slug).

The Udaipur trip content below is lifted 1:1 from the previously hardcoded
website page (app/trips/udaipur/udaipur-client.tsx) — the /trips/[slug]
template renders exactly these shapes. Edit trips in the Supabase dashboard
afterwards; re-running this script resets the seeded rows to this baseline.

Usage: python scripts/seed_group_trips.py
"""
import sys

sys.path.insert(0, ".")

UDAIPUR = {
    "slug": "udaipur",
    "title": "Udaipur, Mount Abu & Kumbhalgarh Group Trip",
    "seo_title": "UDAIPUR + MOUNT ABU / KUMBHALGARH TRIP",
    "description": (
        "Book an affordable Udaipur Group Tour Package from Delhi by AC Traveller "
        "Tempo. 5 Days 4 Nights. Hotel stay, meals, City Palace, Lake Pichola "
        "boating, Bahubali Hills sunrise trek, Mount Abu or Kumbhalgarh Fort included."
    ),
    "destination": "Udaipur",
    "origin": "Delhi",
    "duration_label": "5 Days 4 Nights",
    "duration_days": 5,
    "departure_date": "2026-08-07",
    "departure_label": "7 August 2026",
    "price_from": 7999,
    "whatsapp_number": "918368732396",
    "hero_image": "https://images.unsplash.com/photo-1615836245337-f5b9b2303f10?q=80&w=2000&auto=format&fit=crop",
    "is_active": True,
    "sort": 0,
    "content": {
        "trip_id": "udaipur_group_trip",   # analytics/lead-form id (kept stable)
        "group_types": [
            {"value": "Solo", "emoji": "👤", "defaultMembers": "1"},
            {"value": "Couple", "emoji": "❤️", "defaultMembers": "2"},
            {"value": "Friends", "emoji": "👬", "defaultMembers": "3"},
        ],
        "images": {
            "hero": "https://images.unsplash.com/photo-1615836245337-f5b9b2303f10?q=80&w=2000&auto=format&fit=crop",
            "sunrise": "https://images.unsplash.com/photo-1470071459604-3b5ec3a7fe05?q=80&w=1800&auto=format&fit=crop",
            "boat": "https://images.unsplash.com/photo-1561312514-1d71b2b7e495?q=80&w=1800&auto=format&fit=crop",
            "palace": "https://images.unsplash.com/photo-1622462281019-f6118fc42e46?q=80&w=1800&auto=format&fit=crop",
            "folk": "https://images.unsplash.com/photo-1475483768296-6163e08872a1?q=80&w=1800&auto=format&fit=crop",
            "stars": "https://images.unsplash.com/photo-1419242902214-272b3f66ee7a?q=80&w=2000&auto=format&fit=crop",
            "fort": "https://images.unsplash.com/photo-1599661046289-e31897846e41?q=80&w=1800&auto=format&fit=crop",
            "royal": "https://images.unsplash.com/photo-1477587458883-47145ed94245?q=80&w=2000&auto=format&fit=crop",
            "finale": "https://images.unsplash.com/photo-1631628986548-1bd96d70e748?q=80&w=2000&auto=format&fit=crop",
        },
        "moments": [
            {"img": "sunrise", "emoji": "🌅", "title": "Sunrise at Bahubali Hills",
             "line": "Watch the first light hit the Aravalli Hills."},
            {"img": "boat", "emoji": "🚤", "title": "Boat across Lake Pichola",
             "line": "Glide past palaces that float on water."},
            {"img": "palace", "emoji": "🏰", "title": "Walk the City Palace",
             "line": "Corridors built for kings, 450 years deep."},
            {"img": "folk", "emoji": "💃", "title": "Folk night at Bagore Ki Haveli",
             "line": "Fire, folk songs, and a haveli that comes alive."},
            {"img": "fort", "emoji": "🏔", "title": "Mount Abu or Kumbhalgarh",
             "line": "End it on a hilltop — or on the Great Wall of India."},
        ],
        "route": [
            {"emoji": "🏙", "label": "Delhi"}, {"emoji": "🚐", "label": "Night Traveller"},
            {"emoji": "🏰", "label": "Udaipur"}, {"emoji": "🌅", "label": "Sunrise"},
            {"emoji": "🚤", "label": "Boat Ride"}, {"emoji": "👑", "label": "Royal Palace"},
            {"emoji": "🏔", "label": "Mount Abu"}, {"emoji": "🌃", "label": "Back Home"},
        ],
        "days": [
            {"n": "01", "title": "The Getaway", "rows": [
                {"when": "Evening", "emoji": "🚐", "what": "Board at Akshardham or IFFCO Chowk"},
                {"when": "Night", "emoji": "🌙", "what": "660 km melt away while you sleep"},
            ]},
            {"n": "02", "title": "City of Lakes", "rows": [
                {"when": "Morning", "emoji": "🏨", "what": "Check in. Slow breakfast."},
                {"when": "Afternoon", "emoji": "🏰", "what": "City Palace & Jagdish Temple"},
                {"when": "Evening", "emoji": "🚤", "what": "Boat across Lake Pichola"},
                {"when": "Night", "emoji": "💃", "what": "Folk night at Bagore Ki Haveli"},
                {"when": "Night", "emoji": "🍽️", "what": "Rajasthani dinner under the stars."},
            ]},
            {"n": "03", "title": "The Sunrise Day", "rows": [
                {"when": "Dawn", "emoji": "🌅", "what": "Bahubali Hills sunrise trek"},
                {"when": "Morning", "emoji": "🍳", "what": "Fresh breakfast with lake views."},
                {"when": "Afternoon", "emoji": "🌊", "what": "Badi Lake & Fateh Sagar"},
                {"when": "Evening", "emoji": "🛍", "what": "Get lost in Bapu Bazaar"},
                {"when": "Night", "emoji": "🍛", "what": "Local flavors for a memorable dinner."},
                {"when": "Night", "emoji": "🎧", "what": "Pool party at the rooftop pool"},
            ]},
            {"n": "04", "title": "The Finale", "rows": [
                {"when": "Morning", "emoji": "☕", "what": "One last breakfast before the road calls."},
                {"when": "Day", "emoji": "🏔", "what": "Mount Abu or Kumbhalgarh — the group picks one"},
                {"when": "Evening", "emoji": "🚐", "what": "One last drive. Overnight, home."},
            ]},
            {"n": "05", "title": "Back to Delhi", "rows": [
                {"when": "Morning", "emoji": "🌃", "what": "Arrive 5–10 AM. Camera full. Head full."},
            ]},
        ],
        "pricing": [
            {"title": "Quad", "price": "₹7,999", "value": "Quad Sharing", "amount": 7999},
            {"title": "Triple", "price": "₹8,499", "value": "Triple Sharing", "amount": 8499},
            {"title": "Double", "price": "₹8,999", "value": "Double Sharing", "amount": 8999},
        ],
        "hotel_photos": [
            {"src": "/images/trips/udaipur/hotel-exterior-night.jpeg",
             "alt": "The hotel lit up at night with its rooftop pool sign", "big": True},
            {"src": "/images/trips/udaipur/hotel-pool-waterfall.jpeg",
             "alt": "Rooftop pool with a waterfall feature and loungers"},
            {"src": "/images/trips/udaipur/hotel-room-view.jpeg",
             "alt": "Room with a king bed and Aravalli views from the window"},
            {"src": "/images/trips/udaipur/hotel-pool-day.jpeg",
             "alt": "Rooftop pool under a clear blue sky with mountain views"},
            {"src": "/images/trips/udaipur/hotel-loungers.jpeg",
             "alt": "Cane loungers with towels and drinks by the pool"},
            {"src": "/images/trips/udaipur/hotel-rooftop-dining.jpeg",
             "alt": "Rooftop dining terrace overlooking the Aravalli hills"},
        ],
        "reviews": [
            {"quote": "This trip completely healed my burnout. From watching the sunrise at Bahubali Hills to our late night conversations by the pool, everything was so beautifully managed.",
             "name": "Kritika", "city": "Delhi", "tone": "from-amber-400 to-rose-500"},
            {"quote": "I joined solo and was a bit nervous initially, but the group vibe was incredible. I came back with six close friends and memories to last a lifetime.",
             "name": "Aman", "city": "Gurugram", "tone": "from-sky-400 to-violet-500"},
            {"quote": "Waking up at 4 AM for the sunrise hike was tough, but standing at the peak watching the first golden light hit the Aravalli hills was worth every single second.",
             "name": "Ritu", "city": "Noida", "tone": "from-emerald-400 to-teal-500"},
            {"quote": "No photograph can capture the peace of boating on Lake Pichola at sunset while the City Palace lights start glowing. The itinerary was perfectly balanced.",
             "name": "Sahil", "city": "Delhi", "tone": "from-orange-400 to-amber-500"},
        ],
        "people": [
            {"emoji": "👩", "who": "Solo Traveller", "line": "Arrive alone. Leave with a group chat that never sleeps."},
            {"emoji": "❤️", "who": "Couples", "line": "Let Lake Pichola do the talking."},
            {"emoji": "👬", "who": "Friends", "line": "The trip you've been 'planning' since college."},
            {"emoji": "💻", "who": "Working Professionals", "line": "Out of office. Into the hills."},
        ],
        "inclusions": [
            {"emoji": "🚐", "label": "Traveller Tempo"}, {"emoji": "🏨", "label": "Hotel Stay"},
            {"emoji": "🍽", "label": "Meals"}, {"emoji": "🧭", "label": "Trip Captain"},
            {"emoji": "📍", "label": "Sightseeing"},
        ],
        "fine_print": [
            {"q": "Mount Abu or Kumbhalgarh — who decides?",
             "a": "Day 4 runs as one of the two options for the whole group. The trip captain confirms the finale before departure, based on season and group preference."},
            {"q": "What exactly is included?",
             "a": "AC Traveller Tempo (Delhi ↔ Udaipur ↔ Delhi), hotel stay in Udaipur, breakfasts & dinners, sightseeing vehicle and a trip captain. Boating, monument entry tickets, the folk show ticket and lunches are on you."},
            {"q": "Travelling solo?",
             "a": "Most of the group is. You'll share a room with same-gender travellers unless you pick Double sharing."},
            {"q": "Cancellations & date changes",
             "a": "Cancel 15+ days out: 50% charge. 7–15 days: 75%. Under 7 days: 100%. The advance is non-refundable. Dates can be changed free between 20 and 10 days before departure."},
            {"q": "Pickup & drop",
             "a": "Akshardham Metro and IFFCO Chowk, Delhi NCR. Departure between 5–7 PM. You're back between 5–10 AM on Day 5 (up to ~1 PM with traffic)."},
        ],
    },
}

TRIPS = [UDAIPUR]


def main() -> None:
    from app.db.client import get_db

    db = get_db()
    for trip in TRIPS:
        db.table("group_trips").upsert(trip, on_conflict="slug").execute()
        print(f"upserted /trips/{trip['slug']} — {trip['title']} (from ₹{trip['price_from']})")


if __name__ == "__main__":
    main()
