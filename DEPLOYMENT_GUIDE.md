# Mama's Kitchen - Vercel Deployment Guide

## Overview
This guide covers deploying both the Flask backend and Flutter mobile app.

### Project Structure
- **Backend**: Flask API (`Mama's Kitchen/`)
- **Mobile**: Flutter app (`mamas-kitchen-mobile/`)
- **Database**: Supabase PostgreSQL

---

## Part 1: Flask Backend Deployment to Vercel

### Step 1: Install Vercel CLI
```powershell
npm install -g vercel
```

### Step 2: Prepare Flask for Vercel

Create `vercel.json` in the `Mama's Kitchen` directory:
```json
{
  "version": 2,
  "builds": [
    {
      "src": "app.py",
      "use": "@vercel/python"
    }
  ],
  "routes": [
    {
      "src": "/(.*)",
      "dest": "app.py"
    }
  ],
  "env": {
    "DB_BACKEND": "postgres",
    "SUPABASE_URL": "@supabase_url",
    "SUPABASE_ANON_KEY": "@supabase_anon_key",
    "SUPABASE_SERVICE_ROLE_KEY": "@supabase_service_role_key",
    "SUPABASE_PRODUCT_IMAGE_BUCKET": "product-images",
    "SUPABASE_DOCUMENT_BUCKET": "mamas-kitchen-documents"
  }
}
```

### Step 3: Update requirements.txt
Ensure all dependencies are listed:
```
flask
supabase
psycopg2-binary
python-dotenv
flask-cors
werkzeug
gunicorn
```

### Step 4: Modify app.py for Production
Add this at the top of `app.py`:
```python
import os
from werkzeug.serving import WSGIRequestHandler

# Production settings
if os.getenv('VERCEL'):
    app.wsgi_app = WSGIRequestHandler(app.wsgi_app)
```

### Step 5: Login to Vercel
```powershell
cd "c:\Users\bridd\Downloads\webapp\project\Mama's Kitchen"
vercel login
```
Follow the authentication flow in your browser.

### Step 6: Deploy to Vercel
```powershell
vercel --prod
```

### Step 7: Add Environment Variables
In Vercel Dashboard → Settings → Environment Variables:
```
DB_BACKEND = postgres
SUPABASE_DB_URL = postgresql://postgres:password@db.xxx.supabase.co:5432/postgres
SUPABASE_URL = https://xxx.supabase.co
SUPABASE_ANON_KEY = (from .env)
SUPABASE_SERVICE_ROLE_KEY = (from .env)
SUPABASE_PRODUCT_IMAGE_BUCKET = product-images
SUPABASE_DOCUMENT_BUCKET = mamas-kitchen-documents
```

### Step 8: Verify Deployment
```powershell
vercel --inspect
```

---

## Part 2: Flutter Mobile App Deployment

### Option A: Deploy to Google Play Store (Android)

#### Prerequisites
- Android Studio
- Google Play Developer Account ($25 one-time)
- Keystore file for signing

#### Step 1: Build Release APK
```powershell
cd mamas-kitchen-mobile
flutter build apk --release
```

#### Step 2: Build App Bundle (recommended for Play Store)
```powershell
flutter build appbundle --release
```

Output: `build/app/outputs/bundle/release/app-release.aab`

#### Step 3: Sign the Release Build
Create a keystore (first time only):
```powershell
keytool -genkey -v -keystore c:\keys\mamas_kitchen.keystore `
  -keyalg RSA -keysize 2048 -validity 10000 `
  -alias mamas_kitchen
```

#### Step 4: Create Play Store Signing Configuration
Edit `android/app/build.gradle`:
```gradle
signingConfigs {
    release {
        keyAlias 'mamas_kitchen'
        keyPassword 'YOUR_PASSWORD'
        storeFile file('c:/keys/mamas_kitchen.keystore')
        storePassword 'YOUR_PASSWORD'
    }
}
```

#### Step 5: Upload to Google Play Console
1. Go to https://play.google.com/console
2. Create new app: "Mama's Kitchen"
3. Fill in app details, screenshots, description
4. Upload `app-release.aab` bundle
5. Set pricing (Free)
6. Submit for review (1-3 hours approval)

---

### Option B: Deploy to Apple App Store (iOS)

#### Prerequisites
- macOS with Xcode
- Apple Developer Account ($99/year)
- Apple Certificate and Provisioning Profile

#### Step 1: Build iOS Release
```bash
cd mamas-kitchen-mobile
flutter build ios --release
```

#### Step 2: Create Distribution Certificate
1. Go to Apple Developer Account
2. Create Distribution Certificate
3. Download and install in Keychain

#### Step 3: Create App ID
In Apple Developer Account:
```
Bundle ID: com.mamaskitchen.mobile
App Name: Mama's Kitchen
```

#### Step 4: Create Provisioning Profile
Generate distribution profile for App Store

#### Step 5: Update Xcode Project
1. Open `ios/Runner.xcworkspace` (NOT .xcodeproj)
2. Set Team ID: Your Apple Team ID
3. Set Bundle ID: com.mamaskitchen.mobile

#### Step 6: Archive and Upload
```bash
flutter build ios --release
cd ios
xcodebuild -workspace Runner.xcworkspace -scheme Runner -configuration Release -archivePath build/Runner.xcarchive archive
xcodebuild -exportArchive -archivePath build/Runner.xcarchive -exportOptionsPlist export_options.plist -exportPath build/Runner.ipa
```

#### Step 7: Upload to App Store
1. Open App Store Connect
2. Create new app
3. Upload IPA with Transporter
4. Fill in app details, screenshots, description
5. Submit for review (1-2 days approval)

---

### Option C: Deploy to Firebase Hosting (Web Version)

#### Step 1: Build Web Version
```powershell
cd mamas-kitchen-mobile
flutter build web
```

#### Step 2: Install Firebase CLI
```powershell
npm install -g firebase-tools
```

#### Step 3: Initialize Firebase Project
```powershell
firebase login
firebase init hosting
```

#### Step 4: Deploy to Firebase
```powershell
firebase deploy --only hosting
```

---

## Deployment Checklist

### Before Deploying
- [ ] All environment variables configured
- [ ] Database migrations applied
- [ ] Admin account created
- [ ] API endpoints tested locally
- [ ] Mobile app tested on emulator/device
- [ ] CORS configured correctly
- [ ] SSL certificates valid

### Flask Backend Deployment Checklist
- [ ] `vercel.json` created
- [ ] Environment variables set in Vercel
- [ ] Database connection string verified
- [ ] Static files configured
- [ ] CORS headers set
- [ ] Error logging configured

### Mobile App Deployment Checklist
- [ ] App name and icon finalized
- [ ] Screenshots prepared (5-8 per device size)
- [ ] Privacy policy written
- [ ] Terms of service ready
- [ ] App description (1000+ characters)
- [ ] Rating age determined
- [ ] Contact email set

---

## Post-Deployment Steps

### 1. Test Live Endpoints
```powershell
# Test Flask backend
Invoke-WebRequest -Uri "https://your-vercel-app.vercel.app/api/health"

# Test Supabase connectivity
Invoke-WebRequest -Uri "https://your-vercel-app.vercel.app/api/users" `
  -Headers @{"Authorization"="Bearer YOUR_TOKEN"}
```

### 2. Configure Custom Domain (Optional)
1. Vercel Dashboard → Settings → Domains
2. Add your domain (e.g., api.mamaskitchen.com)
3. Update DNS records

### 3. Set Up Monitoring
- Enable Vercel Analytics
- Configure error tracking
- Set up performance monitoring
- Enable email notifications

### 4. Update App Configuration
In Flutter app, update API endpoint:
```dart
const String API_URL = 'https://your-vercel-app.vercel.app';
```

Rebuild and redeploy mobile app

---

## Troubleshooting

### Flask Backend Issues

**500 Internal Server Error**
```powershell
# Check logs
vercel logs your-project-name --limit 50
```

**Database Connection Failed**
- Verify SUPABASE_DB_URL environment variable
- Check Supabase project is active
- Verify network allows connections

**CORS Errors**
```python
# Add CORS headers in app.py
from flask_cors import CORS
CORS(app)
```

### Mobile App Issues

**Build Failures**
```powershell
flutter clean
flutter pub get
flutter build apk --release
```

**Connection to Backend**
- Verify API URL is correct
- Check CORS configuration
- Test with Postman first

---

## Monitoring & Maintenance

### Daily Checks
- Monitor error rates in Vercel
- Check app store ratings and reviews
- Monitor Supabase query performance

### Weekly Tasks
- Review analytics
- Check for updates needed
- Monitor user feedback

### Monthly Tasks
- Update dependencies
- Review security logs
- Optimize database queries
- Plan feature releases

---

## Rollback Procedure

### Vercel Rollback
```powershell
# View deployment history
vercel ls

# Rollback to previous version
vercel rollback
```

### App Store Updates
1. Create new version number
2. Upload new build
3. Submit for review
4. Takes 1-3 hours for approval

---

## Support & Resources

- Vercel Docs: https://vercel.com/docs
- Flutter Docs: https://flutter.dev/docs
- Google Play: https://play.google.com/console
- App Store: https://appstoreconnect.apple.com
- Supabase: https://supabase.com/docs
