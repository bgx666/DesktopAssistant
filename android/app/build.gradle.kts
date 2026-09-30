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
        versionCode = 22
        // 0.5.6：拍照改成 **App 内自己拍（CameraX）**，不再依赖第三方相机 App
        // （各家 ROM 行为不一致，是"拍了读不出来/没有拍照"的根因）；
        // 日历角标的数字不再被圆形裁剪切掉。
        versionName = "0.5.6"
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
    // 在 App 内自己拍照（CameraX）。
    // 为什么不用系统相机 Intent：那要依赖**第三方相机 App** 的行为——各家 ROM 不一致
    // （有的忽略 EXTRA_OUTPUT、有的拒绝写我们的 URI、有的返回"取消"却写了文件），
    // 用户那边反复失败就是这个原因。自己拍就把相机 App 从链路里彻底去掉。
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
    implementation("androidx.camera:camera-view:1.3.4")

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