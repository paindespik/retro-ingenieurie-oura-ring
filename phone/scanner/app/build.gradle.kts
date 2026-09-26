import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Secrets hors du dépôt : phone/scanner/secrets.properties (gitignoré), sinon
// variables d'environnement. Voir secrets.properties.example.
val secretsFile = rootProject.file("secrets.properties")
val secretsProps = Properties().apply {
    if (secretsFile.exists()) secretsFile.inputStream().use { load(it) }
}
fun secret(key: String, fallback: String = ""): String =
    secretsProps.getProperty(key) ?: System.getenv(key) ?: fallback

android {
    namespace = "io.github.paindespik.ourascan"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.paindespik.ourascan"
        minSdk = 31
        targetSdk = 36
        versionCode = 2
        versionName = "1.1"

        buildConfigField("String", "OURA_SERVER", "\"${secret("OURA_SERVER", "https://oura.example.com")}\"")
        buildConfigField("String", "WEB_USER", "\"${secret("WEB_USER")}\"")
        buildConfigField("String", "WEB_PASS", "\"${secret("WEB_PASS")}\"")
        buildConfigField("String", "PHONE_TOKEN", "\"${secret("PHONE_TOKEN")}\"")
        buildConfigField("String", "RING_SERIAL", "\"${secret("RING_SERIAL")}\"")
    }

    buildFeatures {
        buildConfig = true
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
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
