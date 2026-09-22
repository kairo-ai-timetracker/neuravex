plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "com.neuravex.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.neuravex.app"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
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
        compose = true
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
            // Required when adding web3j — see docs.web3j.io Android setup guide.
            excludes += "/META-INF/DISCLAIMER"
        }
    }
}

dependencies {
    // Web3 / on-chain execution (Polygon + Uniswap V3)
    implementation("org.web3j:core:4.12.0")

    implementation(platform("androidx.compose:compose-bom:2024.09.00"))
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    // Classic (XML/View-system) Material Components library — separate
    // from Compose's material3 artifact above. This app is 100% Compose
    // and never uses classic Material widgets directly, but the base
    // AndroidManifest activity theme (themes.xml's
    // "Theme.Material3.DayNight.NoActionBar") is still an XML resource
    // that only THIS library provides — it's what the OS briefly shows
    // as the window background before setContent() hands control to
    // Compose. Without it, AAPT fails to link that style at all
    // ("resource style/Theme.Material3.DayNight.NoActionBar ... not
    // found"), which is a resource-linking error, not a Kotlin/Compose one.
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.fragment:fragment-ktx:1.8.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.6")
    implementation("androidx.core:core-ktx:1.13.1")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")

    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.squareup.retrofit2:converter-kotlinx-serialization:2.11.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")

    implementation("androidx.datastore:datastore-preferences:1.1.1")
    implementation("com.google.crypto.tink:tink-android:1.18.0")

    implementation("androidx.biometric:biometric:1.1.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")

    testImplementation("junit:junit:4.13.2")
}
