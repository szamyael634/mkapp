# Mama's Kitchen - Quick Start Deployment

## 🚀 Flask Backend to Vercel (5 minutes)

### Step 1: Login to Vercel
```powershell
npm install -g vercel
vercel login
```

### Step 2: Deploy
```powershell
cd "c:\Users\bridd\Downloads\webapp\project\Mama's Kitchen"
vercel --prod
```

### Step 3: Add Environment Variables
Go to Vercel Dashboard → Settings → Environment Variables

Add these from your `.env` file:
```
SUPABASE_DB_URL=postgresql://postgres:password@db.xxx.supabase.co:5432/postgres
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...
SUPABASE_PRODUCT_IMAGE_BUCKET=product-images
SUPABASE_DOCUMENT_BUCKET=mamas-kitchen-documents
DB_BACKEND=postgres
```

### Step 4: Redeploy
```powershell
vercel --prod
```

---

## 📱 Flutter Mobile App Deployment

### For Android (Google Play)

```powershell
cd mamas-kitchen-mobile

# Build release APK
flutter build apk --release

# Build App Bundle (recommended)
flutter build appbundle --release
```

**Upload to Google Play Console:**
1. Go to https://play.google.com/console
2. Create new app
3. Upload `build/app/outputs/bundle/release/app-release.aab`
4. Fill in store listing details
5. Submit for review

### For iOS (App Store)

```bash
cd mamas-kitchen-mobile
flutter build ios --release

# Then in Xcode
# 1. Open ios/Runner.xcworkspace
# 2. Set Team ID
# 3. Archive and upload to App Store Connect
```

**Upload to App Store:**
1. Go to https://appstoreconnect.apple.com
2. Create new app
3. Upload IPA file
4. Fill in app details
5. Submit for review

### For Web (Firebase Hosting)

```powershell
cd mamas-kitchen-mobile
flutter build web

npm install -g firebase-tools
firebase login
firebase init hosting
firebase deploy
```

---

## ✅ Verification Checklist

### After Flask Deployment
- [ ] Visit your Vercel URL (e.g., https://mkapp.vercel.app)
- [ ] Test API endpoint: `/api/users`
- [ ] Check logs in Vercel dashboard
- [ ] Verify database connection works

### Before App Store Submission
- [ ] Update API URL in Flutter app
- [ ] Test login with admin account
- [ ] Verify database operations
- [ ] Test file uploads
- [ ] Check error handling

---

## 🔗 Useful Links

- **Vercel Dashboard**: https://vercel.com/dashboard
- **Google Play Console**: https://play.google.com/console
- **App Store Connect**: https://appstoreconnect.apple.com
- **Firebase Console**: https://console.firebase.google.com
- **Supabase Dashboard**: https://app.supabase.com

---

## 🆘 Troubleshooting

**Flask won't deploy:**
```powershell
# Check for Python compatibility
python --version  # Should be 3.9+

# Verify requirements.txt
vercel env list
```

**Database connection fails:**
```powershell
# Check Supabase is running
# Verify DB URL in environment variables
# Check firewall/network rules
```

**Mobile app can't reach backend:**
```dart
// Update this in main.dart
const String API_URL = 'https://your-vercel-domain.vercel.app';
```

---

## 📞 Support

For issues, check:
1. Vercel logs: `vercel logs --follow`
2. Supabase logs
3. Flutter build output
4. App store review comments
