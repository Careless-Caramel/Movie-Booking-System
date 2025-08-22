import os
import json
from pathlib import Path
from datetime import date, timedelta
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import certifi

from flask import Flask, render_template, request, redirect, url_for
from flask_login import LoginManager, login_user, logout_user, current_user, login_required, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from models import db, Booking, User

# ---------------------------
# Flask & DB
# ---------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = "dev_secret"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///bookings.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)


# ---------------------------
# Flask-Login setup
# ---------------------------
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------------------------
# TMDb config
# ---------------------------
TMDB_API_KEY = "your-api-key"
API_KEY = os.getenv("TMDB_API_KEY") or "your-api-key"
BASE_URL = "https://api.themoviedb.org/3"

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

session = requests.Session()
retries = Retry(
    total=4,
    backoff_factor=0.6,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
session.mount("https://", HTTPAdapter(max_retries=retries))
HEADERS = {"User-Agent": "MovieBook/1.0"}

def tmdb_get(path: str, params: dict):
    params = {"api_key": API_KEY, **(params or {})}
    url = f"{BASE_URL}{path}"
    try:
        r = session.get(
            url,
            params=params,
            headers=HEADERS,
            timeout=12,
            verify=certifi.where(),
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print("TMDb request failed:", e)
        return None

# ---------------------------
# Data access with caching
# ---------------------------
def cache_read(file: Path):
    if file.exists():
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None

def cache_write(file: Path, data):
    try:
        file.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass

def get_recent_movies():
    today = date.today()
    one_week_ago = today - timedelta(days=7)
    cache_file = CACHE_DIR / "recent.json"

    data = tmdb_get(
        "/discover/movie",
        {
            "language": "en-US",
            "sort_by": "primary_release_date.desc",
            "primary_release_date.gte": one_week_ago.isoformat(),
            "primary_release_date.lte": today.isoformat(),
            "vote_average.gte": 4,
            "with_release_type": "2|3",
            "page": 1,
        },
    )

    if data and "results" in data:
        movies = [m for m in data["results"] if m.get("id") and m.get("title") and m.get("poster_path")]
        if movies:
            cache_write(cache_file, movies)
            return movies

    trend = tmdb_get("/trending/movie/week", {"language": "en-US"})
    if trend and "results" in trend:
        movies = [m for m in trend["results"] if m.get("id") and m.get("title") and m.get("poster_path")]
        if movies:
            cache_write(cache_file, movies)
            return movies

    cached = cache_read(cache_file)
    return cached or []

def get_movie_details(movie_id: int):
    cache_file = CACHE_DIR / f"movie_{movie_id}.json"
    data = tmdb_get(f"/movie/{movie_id}", {"language": "en-US"})
    if data and data.get("id") == movie_id:
        cache_write(cache_file, data)
        return data
    return cache_read(cache_file)

# ---------------------------
# Routes
# ---------------------------
GENRE_MAP = {
    28: "Action", 35: "Comedy", 18: "Drama", 27: "Horror",
    10749: "Romance", 878: "Sci-Fi", 16: "Animation",
    53: "Thriller", 12: "Adventure"
}

@app.route("/")
def index():
    query = request.args.get("q", "").strip()
    search_results = []
    if query:
        # Search TMDb API for movies
        import requests
        url = f"https://api.themoviedb.org/3/search/movie"
        params = {"api_key": TMDB_API_KEY, "query": query}
        resp = requests.get(url, params=params)
        if resp.ok:
            data = resp.json()
            search_results = data.get("results", [])
    movies = get_recent_movies()
    genre_movies = defaultdict(list)
    for m in movies:
        for g in m.get("genre_ids", []):
            genre_movies[GENRE_MAP.get(g, "Other")].append(m)
    return render_template(
        "index.html",
        movies=movies,
        genre_movies=genre_movies,
        search_results=search_results,
        search_query=query
    )

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/movie/<int:movie_id>", methods=["GET", "POST"])
@login_required
def movie_page(movie_id):
    booking_success = False
    message = None
    message_type = None
    if request.method == "POST":
        movie_title = request.form.get("movie_title") or "Unknown"
        seats = request.form.get("seats")
        showtime = request.form.get("showtime")
        date_selected = request.form.get("date")

        if not (seats and showtime and date_selected):
            message = "🎬 Please complete all fields to book your cinematic adventure!"
            message_type = "warning"
        else:
            # Prevent duplicate booking for same user, movie, showtime, and date
            existing = Booking.query.filter_by(
                movie_id=movie_id,
                user_id=current_user.id,
                showtime=showtime,
                date=date_selected
            ).first()
            if existing:
                message = "🚫 You have already booked this movie for the selected showtime and date."
                message_type = "error"
            else:
                from datetime import datetime
                booking = Booking(
                    user_id=current_user.id,
                    movie_id=movie_id,
                    movie_title=movie_title,
                    name=current_user.name,
                    email=current_user.email,
                    seats=int(seats),
                    showtime=showtime,
                    date=date_selected,
                    created_at=datetime.now(),
                )
                db.session.add(booking)
                db.session.commit()
                booking_success = True
                message = "🍿 Booking successful! Get ready for a blockbuster experience."
                message_type = "success"

    movie = get_movie_details(movie_id)
    if not movie:
        return render_template("movie.html", movie=None, booking_success=False, available_dates=[], message="🚫 Movie details unavailable right now. Please try again later.", message_type="error")

    from datetime import date, timedelta
    available_dates = [(date.today() + timedelta(days=i)).isoformat() for i in range(7)]
    return render_template(
        "movie.html",
        movie=movie,
        booking_success=booking_success,
        available_dates=available_dates,
        message=message,
        message_type=message_type
    )

# ---------------------------
# Auth Routes
# ---------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    message = None
    message_type = None
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        password = generate_password_hash(request.form["password"])

        if User.query.filter_by(email=email).first():
            message = "⚠️ Email already registered! Try logging in."
            message_type = "danger"
        else:
            user = User(name=name, email=email, password=password)
            db.session.add(user)
            db.session.commit()
            # Redirect to login with success message
            return redirect(url_for("login", message="✅ Account created! Please log in to start booking.", message_type="success"))
    return render_template("register.html", message=message, message_type=message_type)

@app.route("/login", methods=["GET", "POST"])
def login():
    message = request.args.get("message")
    message_type = request.args.get("message_type")
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            message = "👋 Welcome back! You are now logged in."
            message_type = "success"
            return redirect(url_for("dashboard"))
        else:
            message = "❌ Invalid email or password. Please try again."
            message_type = "danger"
    return render_template("login.html", message=message, message_type=message_type)

@app.route("/logout")
def logout():
    logout_user()
    return render_template("index.html", message="👋 You have been logged out. See you next time!", message_type="info", movies=get_recent_movies(), genre_movies=defaultdict(list))

@app.route("/dashboard")
@login_required
def dashboard():
    # Show all bookings for debugging. Change back to user-specific by filtering with email=current_user.email
    bookings = Booking.query.all()
    for b in bookings:
        movie = get_movie_details(b.movie_id)
        b.poster_url = (
            f"https://image.tmdb.org/t/p/w200{movie['poster_path']}"
            if movie and movie.get("poster_path")
            else "/static/img/placeholder.png"
        )
    return render_template("dashboard.html", bookings=bookings)

@app.route('/cancel/<int:booking_id>', methods=['POST'])
@login_required
def cancel_booking(booking_id):
    booking = Booking.query.filter_by(id=booking_id, email=current_user.email).first()
    if booking:
        db.session.delete(booking)
        db.session.commit()
        return render_template("booking_cancelled.html", message="❌ Booking Cancelled — Your ticket has been released!", message_type="error")
    return redirect(url_for('dashboard'))

@app.route("/booking_cancelled")
def booking_cancelled():
    return render_template("booking_cancelled.html", message="❌ Booking Cancelled — Your ticket has been released!", message_type="error")

# ---------------------------
# Main
# ---------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=2000)

