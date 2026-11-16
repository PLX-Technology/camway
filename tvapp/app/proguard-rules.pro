# CamWay TV — reglas de R8.
# libVLC usa JNI: el código nativo llama a clases y métodos Java por nombre.
-keep class org.videolan.** { *; }
-dontwarn org.videolan.**
# Media3/ExoPlayer trae sus propias reglas (consumer rules).
