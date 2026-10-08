import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing is read from a properties file that lives OUTSIDE the repo
// (storeFile, storePassword, keyAlias, keyPassword). Point at it with the env var
// JOBAGG_SIGNING_PROPERTIES or -PsigningProperties=/path/keystore.properties.
// Without it, `assembleRelease` produces an unsigned APK; CI builds debug only.
val signingPropsPath: String? =
    (project.findProperty("signingProperties") as String?) ?: System.getenv("JOBAGG_SIGNING_PROPERTIES")
val signingProps = Properties().apply {
    signingPropsPath?.let { p -> file(p).takeIf { it.exists() }?.inputStream()?.use { load(it) } }
}
val hasReleaseSigning = signingProps.getProperty("storeFile") != null

android {
    namespace = "com.gtownrter77.jobaggregator"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.gtownrter77.jobaggregator"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
        resValue("string", "app_name", "Job Aggregator")
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = file(signingProps.getProperty("storeFile"))
                storePassword = signingProps.getProperty("storePassword")
                keyAlias = signingProps.getProperty("keyAlias")
                keyPassword = signingProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (hasReleaseSigning) signingConfig = signingConfigs.getByName("release")
        }
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
            resValue("string", "app_name", "Job Aggregator (debug)")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        buildConfig = true
    }
    lint {
        abortOnError = true
        checkReleaseBuilds = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
    testImplementation("junit:junit:4.13.2")
}
