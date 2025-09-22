from fastapi import FastAPI, Request, HTTPException, Response, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google_auth_oauthlib.flow import Flow
import os
from urllib.parse import urlencode, parse_qs
import secrets
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer
import json
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import logging

# Load environment variables
load_dotenv()

app = FastAPI()

# Session configuration
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-here-change-this-in-production")
serializer = URLSafeTimedSerializer(SECRET_KEY)

# OAuth Configuration
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'  # Only for development
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# Try different common redirect URIs - update this to match your Google Console setup
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8000/auth/google/callback")

# Client configuration
client_config = {
    "web": {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT_URI]
    }
}

# MongoDB Configuration
MONGODB_URL = os.getenv("MONGODB_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME", "opencraft")

# Initialize MongoDB client
try:
    client = MongoClient(MONGODB_URL)
    db = client[DATABASE_NAME]
    users_collection = db.users
    # Test the connection
    client.admin.command('ping')
    logging.info("Connected to MongoDB successfully")
except ConnectionFailure as e:
    logging.error(f"Failed to connect to MongoDB: {e}")
    # You might want to handle this more gracefully in production

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates
templates = Jinja2Templates(directory="templates")

# Session helpers
pending_auth_sessions = {}  # Store pending authentication sessions

def get_session_data(request: Request):
    """Extract session data from cookies"""
    session_cookie = request.cookies.get("session")
    if not session_cookie:
        return None
    
    try:
        session_data = serializer.loads(session_cookie, max_age=86400)  # 24 hours
        return session_data
    except:
        return None

def set_session_cookie(response: Response, user_data: dict):
    """Set session cookie with user data"""
    session_token = serializer.dumps(user_data)
    response.set_cookie(
        key="session",
        value=session_token,
        max_age=86400,  # 24 hours
        httponly=True,
        secure=False,  # Set to True in production with HTTPS
        samesite="lax"
    )

def clear_session_cookie(response: Response):
    """Clear session cookie"""
    response.delete_cookie("session")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    user_data = get_session_data(request)
    return templates.TemplateResponse("landing.html", {"request": request, "user": user_data})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user_data = get_session_data(request)
    return templates.TemplateResponse("login.html", {"request": request, "user": user_data})

@app.get("/signin", response_class=HTMLResponse)
async def signin_page(request: Request):
    user_data = get_session_data(request)
    return templates.TemplateResponse("signin.html", {"request": request, "user": user_data})

@app.get("/character", response_class=HTMLResponse)
async def character_page(request: Request):
    user_data = get_session_data(request)
    if not user_data or not user_data.get('authenticated'):
        return RedirectResponse(url="/login", status_code=302)
    # Get user character from database
    user_doc = users_collection.find_one({"email": user_data["email"]})
    character_name = None
    if user_doc:
        character_name = user_doc.get("character_name")
    
    return templates.TemplateResponse("character.html", {
        "request": request, 
        "user": user_data,
        "character_name": character_name
    })


@app.get("/download", response_class=HTMLResponse)
async def download_page(request: Request):
    user_data = get_session_data(request)
    return templates.TemplateResponse("download.html", {"request": request, "user": user_data})


@app.post("/api/character/create")
async def create_character(request: Request, character_name: str = Form(...)):
    """Create or update character name for the logged-in user"""
    user_data = get_session_data(request)
    if not user_data or not user_data.get('authenticated'):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        # Update or create user document with character name
        result = users_collection.update_one(
            {"email": user_data["email"]},
            {
                "$set": {
                    "email": user_data["email"],
                    "name": user_data["name"],
                    "picture": user_data["picture"],
                    "character_name": character_name
                }
            },
            upsert=True
        )
        
        return JSONResponse({
            "success": True,
            "message": "Character name saved successfully",
            "character_name": character_name
        })
    except Exception as e:
        logging.error(f"Error saving character name: {e}")
        raise HTTPException(status_code=500, detail="Failed to save character name")

@app.get("/auth/google")
async def google_auth(callback: str = None):
    """Redirect to Google OAuth"""
    flow = Flow.from_client_config(client_config, scopes=[
        'openid',
        'https://www.googleapis.com/auth/userinfo.email',
        'https://www.googleapis.com/auth/userinfo.profile'
    ])
    flow.redirect_uri = REDIRECT_URI
    
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='select_account'
    )
    
    # Store launcher callback URL if provided
    if callback:
        pending_auth_sessions[state] = {'launcher_callback': callback}
    
    return RedirectResponse(url=authorization_url)

@app.get("/auth/google/callback")
async def google_callback(request: Request):
    """Handle Google OAuth callback"""
    try:
        flow = Flow.from_client_config(client_config, scopes=[
            'openid',
            'https://www.googleapis.com/auth/userinfo.email',
            'https://www.googleapis.com/auth/userinfo.profile'
        ])
        flow.redirect_uri = REDIRECT_URI
        
        # Get the authorization code from the callback
        authorization_response = str(request.url)
        flow.fetch_token(authorization_response=authorization_response)
        
        # Get user info from the token
        credentials = flow.credentials
        request_session = google_requests.Request()
        
        # Verify the token and get user info
        idinfo = id_token.verify_oauth2_token(
            credentials.id_token,
            request_session,
            GOOGLE_CLIENT_ID
        )
        
        # Extract user information
        user_email = idinfo.get('email')
        user_name = idinfo.get('name')
        user_picture = idinfo.get('picture')
        
        # Create session data
        user_data = {
            'email': user_email,
            'name': user_name,
            'picture': user_picture,
            'authenticated': True
        }
        
        # Check if this was called from launcher using state
        state = request.query_params.get('state')
        launcher_callback = None
        
        if state and state in pending_auth_sessions:
            session_data = pending_auth_sessions.pop(state)  # Remove from pending
            launcher_callback = session_data.get('launcher_callback')
        
        # If launcher callback, redirect back to launcher
        if launcher_callback:
            # Get user's character name from database
            user_doc = users_collection.find_one({"email": user_email})
            character_name = None
            if user_doc:
                character_name = user_doc.get("character_name")
            
            # Build callback URL with both username and character name
            callback_url = f"{launcher_callback}?success=true&username={user_email}"
            if character_name:
                callback_url += f"&character_name={character_name}"
            
            return RedirectResponse(url=callback_url, status_code=302)
        
        # Otherwise, normal web flow
        response = RedirectResponse(url="/", status_code=302)
        set_session_cookie(response, user_data)
        
        return response
        
    except Exception as e:
        # If launcher callback and error occurred, redirect back with error
        state = request.query_params.get('state')
        if state and state in pending_auth_sessions:
            session_data = pending_auth_sessions.pop(state)  # Remove from pending
            launcher_callback = session_data.get('launcher_callback')
            if launcher_callback:
                callback_url = f"{launcher_callback}?success=false&error={str(e)}"
                return RedirectResponse(url=callback_url, status_code=302)
        
        raise HTTPException(status_code=400, detail=f"Authentication failed: {str(e)}")

@app.get("/logout")
async def logout(request: Request):
    """Handle user logout"""
    response = RedirectResponse(url="/", status_code=302)
    clear_session_cookie(response)
    return response

@app.get("/hello/{name}")
def read_item(name: str):
    return {"message": f"Hello {name}"}
