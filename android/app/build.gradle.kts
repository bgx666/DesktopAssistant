plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("com.google.devtools.ksp")
}

android {
    namespace = "com.xiaozhu.planner"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.xiaozhu.planner"
        minSdk = 26
        targetSdk = 35
        versionCode = 21
        // 0.5.5：拍照输出改走相册（MediaStore，厂商相机兼容性更好）；相机返回"取消"时
        // 也先看文件（有些 ROM 写了文件仍返回取消）；相册选图改为先落本地再解码
        // （云相册/HEIC/一次性流直接解会失败）；复制/朗读移到气泡下方靠右。
        versionName = "0.5.5"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
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
        // 需要 BuildConfig.VERSION_NAME：自报 User-Agent 要用它
        // （opencode 的网关明确拒绝通用库名，见 core/llm/EndpointUrl.kt）
        buildConfig = true
    }
    packaging {
        resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
        // Robolectric 需要真实资源与 manifest 才能跑 Room 测试
        unitTests.isIncludeAndroidResources = true
    }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2024.10.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.core:core-ktx:1.13.1")
    // 读 EXIF 摆正照片方向（手机竖拍的像素常是横的，不摆正模型看到的就是躺着的图）
    implementation("androidx.exifinterface:exifinterface:1.3.7")

    // 网络 + JSON：直连 OpenAI 兼容端点（DeepSeek），SSE 流式 + tool calls
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // 本地记忆/日历：Room
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")

    // 密钥加密存储（LLM key 不上明文盘）
    implementation("androidx.security:security-crypto:1.0.0")

    debugImplementation("androidx.compose.ui:ui-tooling")

    testImplementation("junit:junit:4.13.2")
    // Room 的单测要在 JVM 上跑 Android 环境 → Robolectric
    testImplementation("org.robolectric:robolectric:4.13")
    testImplementation("androidx.test:core:1.6.1")
    testImplementation("androidx.test.ext:junit:1.2.1")
    testImplementation("androidx.room:room-testing:2.6.1")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")

    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation(platform("androidx.compose:compose-bom:2024.10.01"))
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
}