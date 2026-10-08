# Job Aggregator for Android

<img src="icon-512.png" width="96" align="right" alt="app icon">

A thin Android client for the [job aggregator](../README.md). The phone can't run JobSpy or
Ollama, so the app shows the aggregator's web UI from **your own computer** over home Wi-Fi.
How to install and connect it: [README → Phone app](../README.md#phone-app-android).

* Kotlin, one `WebView` activity + a settings screen; no third-party libraries beyond AndroidX
* Package `com.gtownrter77.jobaggregator`, minSdk 24 (Android 7.0), targetSdk 34
* First launch asks for the server address (e.g. `http://192.168.1.50:8765`) and the optional
  access token, with **Test connection** (`/healthz`, then `/api/stats` with the token) and
  plain-language errors. Both are saved in SharedPreferences (excluded from backups) and can be
  changed from the ⋮ menu.
* The token is sent as the `X-Access-Token` header and an `agg_token` cookie, so htmx requests
  are authorized too.
* Pull-to-refresh; back = WebView history; pages of the server stay in the app, everything else
  (job postings, company sites, `mailto:`, `tel:`) opens in the phone's browser / email / dialer.
* Cleartext HTTP is allowed (`res/xml/network_security_config.xml`) because the server is plain
  HTTP on your LAN. Android can't limit that to private IP ranges, so Settings warns when the
  address isn't a home-network / VPN one.
* The app never sends email; it only shows the UI, where approving a draft just marks it.

## Build

Requirements: JDK 17 and the Android SDK (`platform-tools`, `platforms;android-34`,
`build-tools;34.0.0`). Point `ANDROID_HOME` at the SDK (or write `sdk.dir=...` into
`android/local.properties`, which is gitignored).

```bash
cd android
./gradlew testDebugUnitTest      # JVM unit tests (address parsing, connection test vs a fake server)
./gradlew assembleDebug          # app/build/outputs/apk/debug/app-debug.apk  (package ...jobaggregator.debug)
JOBAGG_SIGNING_PROPERTIES=/path/to/keystore.properties ./gradlew assembleRelease
                                 # app/build/outputs/apk/release/app-release.apk (signed)
```

Debug builds use the package `com.gtownrter77.jobaggregator.debug` and the label
"Job Aggregator (debug)", so they install next to the release app.

CI (`.github/workflows/android.yml`) runs the unit tests + lint and uploads a **debug** APK as a
build artifact on every push that touches `android/**`. It has no access to the release key.

## Release signing (keep the key!)

Release APKs are signed with a key that is **never committed** and **not stored in GitHub**.
On the build machine it lives outside the repo, e.g.

```
/workspace/job-aggregator-secrets/            (mode 700)
  jobaggregator-release.jks                    (mode 600, PKCS12, alias "jobaggregator")
  keystore.properties                          (mode 600)
```

`keystore.properties`:

```properties
storeFile=/absolute/path/jobaggregator-release.jks
storePassword=...
keyAlias=jobaggregator
keyPassword=...
```

**Back up both files somewhere safe (password manager / encrypted drive).** Android only installs
an update over the existing app if it's signed with the *same* key. If the key is lost, the
next version has to be installed after uninstalling the old app (which also forgets the saved
server address).

Creating a new key (only for a brand-new app identity):

```bash
keytool -genkeypair -keystore jobaggregator-release.jks -storetype PKCS12 -alias jobaggregator \
        -keyalg RSA -keysize 4096 -validity 36500 -dname "CN=Your Name, O=Job Aggregator, C=US"
```

## Publishing an update

1. Bump `versionCode` (+1) and `versionName` in `app/build.gradle.kts`.
2. `JOBAGG_SIGNING_PROPERTIES=... ./gradlew testDebugUnitTest assembleRelease`
3. Check it: `apksigner verify --print-certs app/build/outputs/apk/release/app-release.apk`
   and `aapt dump badging app/build/outputs/apk/release/app-release.apk`
4. `cp app/build/outputs/apk/release/app-release.apk ../dist/JobAggregator.apk` (dist/ is gitignored)
5. `gh release create apk-vX.Y ../dist/JobAggregator.apk --title "Android app vX.Y" --notes "..."`

Installing the new APK over the old one keeps the app's settings.
