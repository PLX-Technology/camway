package com.pacific.camwaytv;

import android.app.Activity;
import android.app.AlarmManager;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.provider.Settings;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.GridLayout;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.content.res.ColorStateList;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import android.view.TextureView;
import android.view.SurfaceView;

import androidx.annotation.OptIn;
import androidx.media3.common.MediaItem;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.common.util.UnstableApi;
import androidx.media3.common.Format;
import androidx.media3.exoplayer.DefaultLoadControl;
import androidx.media3.exoplayer.DefaultRenderersFactory;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.rtsp.RtspMediaSource;
import androidx.media3.exoplayer.video.VideoFrameMetadataListener;

import org.json.JSONArray;
import org.json.JSONObject;
import org.videolan.libvlc.LibVLC;
import org.videolan.libvlc.Media;
import org.videolan.libvlc.MediaPlayer;
import org.videolan.libvlc.util.VLCVideoLayout;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.CookieHandler;
import java.net.CookieManager;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * CamWay TV — reproductor nativo de cámaras en TIEMPO REAL por RTSP (libVLC).
 * Pensado para el perforador: baja latencia (~medio segundo), decodificado por
 * hardware, liviano para televisores de poca RAM. Al maximizar una cámara se
 * apagan las demás para liberar el decodificador.
 */
public class MainActivity extends Activity {

    private LibVLC libVLC;
    private GridLayout grid;
    private FrameLayout root;
    private final List<Tile> tiles = new ArrayList<>();
    private int cols = 1, focus = 0, maxed = -1;
    private boolean loaded = false;
    private FrameLayout splash;
    private long splashShownAt;
    private static final long SPLASH_MIN_MS = 6000;   // que se alcance a ver la marca

    // Sondeo para tomar al vuelo reasignaciones de cámaras del admin.
    private static final long POLL_MS = 30000;
    private String currentSig = "";
    private final Handler poll = new Handler(Looper.getMainLooper());
    private final Runnable pollTask = new Runnable() {
        @Override public void run() { checkForChanges(); poll.postDelayed(this, POLL_MS); }
    };

    // Reconexión automática si una cámara se cae.
    private static final long RECONNECT_MS = 4000;
    private final Handler vlcHandler = new Handler(Looper.getMainLooper());

    // Vista a pantalla completa para la cámara maximizada (en alta) o una grabación.
    private LinearLayout centerLogo;     // símbolo + "CamWay" al centro (solo con 4 cámaras)
    private VLCVideoLayout maxVlc;       // SOLO para reproducir grabaciones (libVLC)
    private TextureView maxView;         // vivo maximizado (ExoPlayer)
    private ExoPlayer maxExo;            // reproductor del vivo maximizado
    private MediaPlayer recPlayer;       // grabaciones (libVLC)

    private static class Tile {
        String title, rtsp, rtspHi, deviceId;
        FrameLayout cell;
        VLCVideoLayout vout;   // vista de video libVLC del tile (muro)
        MediaPlayer vlc;       // reproductor libVLC del tile (decodificador avcodec eficiente)
        int failCount;         // backoff de scheduleReconnect
        volatile long lastOkAt;   // último evento TimeChanged (el video avanza) → vivo
        // El vivo H.264 maximizado usa ExoPlayer (maxExo).
    }

    private static class Rec {
        String name, stream, kind; long size; double mtime;
    }

    // Menú/grabaciones (overlay sobre el muro, navegable con el control).
    private String baseUrl = "", sessionToken = "";
    private View panel = null;                 // overlay visible (menú o lista), o null
    private Runnable onPanelBack = null;       // qué hace ATRÁS dentro del overlay
    private boolean recPlaying = false;        // reproduciendo una grabación a pantalla completa
    private boolean autostartAsked = false;    // para no repetir el aviso en el mismo arranque

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        installCrashWatchdog();
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        CookieHandler.setDefault(new CookieManager());   // mantiene la sesión

        ArrayList<String> opts = new ArrayList<>();
        opts.add("--no-audio");
        opts.add("--rtsp-tcp");
        opts.add("--network-caching=100");
        opts.add("--live-caching=100");
        opts.add("--clock-jitter=0");
        opts.add("--clock-synchro=0");
        opts.add("--drop-late-frames");
        opts.add("--skip-frames");
        // Ajuste fino (diagnóstico de latencia): opciones extra de libVLC separadas por coma desde las
        // preferencias ("vlc_opts"). Vacío por defecto = comportamiento normal. Las últimas ganan.
        for (String o : getSharedPreferences("camway", MODE_PRIVATE).getString("vlc_opts", "").split(",")) {
            if (!o.trim().isEmpty()) opts.add(o.trim());
        }
        libVLC = new LibVLC(this, opts);

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        grid = new GridLayout(this);
        root.addView(grid, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        // Símbolo CamWay + "CamWay" al centro, en el cruce de las 4 cámaras (vista
        // 2x2). Sin caja de fondo; con sombra para leerse sobre el video.
        float dens = getResources().getDisplayMetrics().density;
        centerLogo = new LinearLayout(this);
        centerLogo.setOrientation(LinearLayout.HORIZONTAL);
        centerLogo.setGravity(Gravity.CENTER_VERTICAL);
        ImageView mark = new ImageView(this);
        mark.setImageResource(R.drawable.logo_mark);
        int mk = (int) (42 * dens);
        LinearLayout.LayoutParams mkp = new LinearLayout.LayoutParams(mk, mk);
        mkp.rightMargin = (int) (9 * dens);
        centerLogo.addView(mark, mkp);
        TextView txt = new TextView(this);
        android.text.SpannableStringBuilder sb = new android.text.SpannableStringBuilder("CamWay");
        sb.setSpan(new android.text.style.ForegroundColorSpan(0xFFFFFFFF), 0, 3, 0);
        sb.setSpan(new android.text.style.ForegroundColorSpan(0xFF22D3EE), 3, 6, 0);
        txt.setText(sb);
        txt.setTextSize(26);
        txt.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        txt.setShadowLayer(8, 0, 2, 0xCC000000);
        centerLogo.addView(txt);
        centerLogo.setAlpha(0.9f);
        centerLogo.setVisibility(View.GONE);
        FrameLayout.LayoutParams clp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER);
        root.addView(centerLogo, clp);

        root.addView(buildSplash());
        setContentView(root);
    }

    /** Reprograma el reinicio de la app ANTES de dejarla morir, si algo no
     * capturado la tumba (NullPointerException, OutOfMemoryError al abrir
     * demasiados decodificadores a la vez, etc.). Sin esto, un crash en un
     * taladro sin personal técnico deja la TV en el launcher indefinidamente:
     * nada la vuelve a abrir sola. Cubre Exception Y Error (OOM es Error, no
     * Exception — el resto del código solo atrapaba Exception). */
    private void installCrashWatchdog() {
        final Thread.UncaughtExceptionHandler previous = Thread.getDefaultUncaughtExceptionHandler();
        final Context appCtx = getApplicationContext();
        Thread.setDefaultUncaughtExceptionHandler((thread, ex) -> {
            try {
                android.util.Log.e("CamWayTV", "Crash no capturado; reprogramando reinicio en 3s", ex);
                Intent relaunch = new Intent(appCtx, MainActivity.class);
                relaunch.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
                int flags = PendingIntent.FLAG_ONE_SHOT
                        | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
                PendingIntent pi = PendingIntent.getActivity(appCtx, 0, relaunch, flags);
                AlarmManager am = (AlarmManager) appCtx.getSystemService(Context.ALARM_SERVICE);
                // set() normal (no exacto): no requiere el permiso especial de
                // "alarmas exactas" de Android 12+, que un kiosko sin nadie que
                // apruebe dialogos de permisos nunca podria conceder. Unos
                // segundos de margen no importan para este relanzamiento.
                long when = android.os.SystemClock.elapsedRealtime() + 3000;
                if (am != null) {
                    am.set(AlarmManager.ELAPSED_REALTIME_WAKEUP, when, pi);
                }
            } catch (Throwable t2) {
                // si hasta programar el reinicio falla, no hay mas remedio que dejarla morir
            } finally {
                if (previous != null) {
                    previous.uncaughtException(thread, ex);
                } else {
                    android.os.Process.killProcess(android.os.Process.myPid());
                    System.exit(10);
                }
            }
        });
    }

    /** Pantalla de carga con el logo; se cubre el muro hasta que haya cámaras (o error). */
    private View buildSplash() {
        splash = new FrameLayout(this);
        splashShownAt = android.os.SystemClock.elapsedRealtime();
        ImageView img = new ImageView(this);
        img.setImageResource(R.drawable.splash);
        img.setScaleType(ImageView.ScaleType.CENTER_CROP);
        splash.addView(img, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        ProgressBar pb = new ProgressBar(this);
        pb.setIndeterminateTintList(ColorStateList.valueOf(Color.parseColor("#22D3EE")));
        int sz = (int) (getResources().getDisplayMetrics().density * 44);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(sz, sz, Gravity.CENTER_HORIZONTAL | Gravity.BOTTOM);
        lp.bottomMargin = (int) (getResources().getDisplayMetrics().heightPixels * 0.228f);
        splash.addView(pb, lp);
        return splash;
    }

    private void hideSplash(long delayMs) {
        if (splash == null) return;
        final View s = splash;
        splash = null;
        long left = SPLASH_MIN_MS - (android.os.SystemClock.elapsedRealtime() - splashShownAt);
        delayMs = Math.max(delayMs, left);
        s.postDelayed(() -> s.animate().alpha(0f).setDuration(350)
                .withEndAction(() -> root.removeView(s)).start(), delayMs);
    }

    @Override
    protected void onResume() {
        super.onResume();
        immersive();
        SharedPreferences p = getSharedPreferences("camway", MODE_PRIVATE);
        String ip = p.getString("ip", "");
        boolean manual = "manual".equals(p.getString("source", "server"));
        boolean ready = manual ? ManualCams.load(this).length() > 0
                               : (ip != null && !ip.trim().isEmpty());
        if (!ready) {
            startActivity(new Intent(this, SetupActivity.class));
            return;
        }
        if (!loaded) loadWall();
        else if (maxed < 0) {
            restartGrid();        // reanuda lo que ya había, al instante
            checkForChanges();    // y re-lee la lista YA (p.ej. una cámara recién agregada)
        }
        startPolling();      // detectar reasignaciones del admin al vuelo
        if (canAutostart()) WakeService.start(this);   // reabrir al despertar el TV
        maybeOfferAutostart();
    }

    // ---------------- inicio automático al encender el TV ----------------
    /** ¿Puede la app lanzarse sola desde el arranque? En Android 10+ hace falta
     *  el permiso "Mostrar sobre otras apps" para saltarse la restricción de
     *  lanzar actividades desde segundo plano. */
    private boolean canAutostart() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(this);
    }

    /** Si aún no se puede autoarrancar, ofrece (una vez por sesión) abrir el ajuste
     *  del sistema para concederlo. */
    private void maybeOfferAutostart() {
        if (autostartAsked || canAutostart()) return;
        autostartAsked = true;
        new AlertDialog.Builder(this)
            .setTitle("Inicio automático")
            .setMessage("Para que CamWay abra sola al encender el televisor, activa el "
                    + "permiso \"Mostrar sobre otras apps\" (o \"Aparecer encima\") para CamWay TV.\n\n"
                    + "Se abrirá la pantalla de ajustes del sistema; activa el interruptor y vuelve atrás.")
            .setPositiveButton("Activar", (d, w) -> openOverlaySettings())
            .setNegativeButton("Ahora no", null)
            .show();
    }

    private void openOverlaySettings() {
        try {
            Intent i = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:" + getPackageName()));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (Exception e) {
            try { startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION)); }
            catch (Exception e2) {
                Toast.makeText(this, "Abre Ajustes › Apps › Acceso especial › "
                        + "Mostrar sobre otras apps › CamWay TV", Toast.LENGTH_LONG).show();
            }
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        stopPolling();
        stopAll();   // libera decodificadores al salir
    }

    // ---------------- carga del muro (login + /api/wall) ----------------
    /** Trae la lista de cámaras (reintenta con login si la sesión caducó). Bloqueante. */
    private List<Tile> fetchTilesBlocking() throws Exception {
        SharedPreferences p = getSharedPreferences("camway", MODE_PRIVATE);
        if ("manual".equals(p.getString("source", "server"))) {
            return buildManualTiles();     // cámaras directas por RTSP, sin servidor
        }
        baseUrl = "http://" + p.getString("ip", "").trim() + ":" + p.getString("port", "8120").trim();
        String user = p.getString("user", "");
        String pass = p.getString("pass", "");
        String json;
        try {
            json = get(baseUrl + "/api/wall");             // sesión vigente
        } catch (Exception first) {
            if (!user.isEmpty()) {                         // caducó → login y reintento
                JSONObject body = new JSONObject();
                body.put("username", user);
                body.put("password", pass);
                post(baseUrl + "/api/login", body.toString());
            }
            json = get(baseUrl + "/api/wall");
        }
        captureToken();
        List<Tile> got = new ArrayList<>();
        JSONArray arr = new JSONObject(json).getJSONArray("tiles");
        for (int i = 0; i < arr.length(); i++) {
            JSONObject t = arr.getJSONObject(i);
            String rtsp = t.optString("rtsp", "");
            if (rtsp.isEmpty()) continue;
            Tile tl = new Tile();
            tl.title = t.optString("title", "");
            tl.rtsp = rtsp;
            tl.rtspHi = t.optString("rtsp_hi", rtsp);
            tl.deviceId = t.optString("device_id", "");
            got.add(tl);
            if (got.size() >= 9) break;   // tope para no ahogar el TV
        }
        return got;
    }

    /** Arma la grilla desde las cámaras MANUALES (RTSP directo, sin servidor). */
    private List<Tile> buildManualTiles() {
        List<Tile> got = new ArrayList<>();
        JSONArray arr = ManualCams.load(this);
        for (int i = 0; i < arr.length() && got.size() < 9; i++) {
            JSONObject c = arr.optJSONObject(i);
            if (c == null) continue;
            String url = ManualCams.buildUrl(c);
            Tile tl = new Tile();
            tl.title = c.optString("name", "").trim();
            if (tl.title.isEmpty()) tl.title = c.optString("ip", "Cámara " + (i + 1));
            tl.rtsp = url;          // muro: lo configurado (el substream 102)
            tl.rtspHi = hiUrl(url); // maximizar: deriva el flujo principal (1080p) si se reconoce el patrón
            tl.deviceId = "";      // sin servidor → sin grabaciones/PTZ
            got.add(tl);
        }
        return got;
    }

    /** Deriva la URL del flujo PRINCIPAL (alta resolución) a partir de la del substream,
     *  para cámaras cuyo patrón de ruta se reconoce. Si no se reconoce, devuelve la misma
     *  (al maximizar se verá el substream, sin romper nada). */
    private static String hiUrl(String sub) {
        if (sub == null) return null;
        // Hikvision/HiLook: /Streaming/channels/102 (sub) -> 101 (main)
        if (sub.contains("/Streaming/channels/102")) return sub.replace("/Streaming/channels/102", "/Streaming/channels/101");
        if (sub.contains("/Streaming/Channels/102")) return sub.replace("/Streaming/Channels/102", "/Streaming/Channels/101");
        // Dahua/estilo realmonitor: subtype=1 (sub) -> subtype=0 (main)
        if (sub.contains("subtype=1")) return sub.replace("subtype=1", "subtype=0");
        return sub;
    }

    /** Lee el token de sesión de la cookie (para pasárselo a libVLC por ?token=). */
    private void captureToken() {
        try {
            java.net.CookieStore cs = ((CookieManager) CookieHandler.getDefault()).getCookieStore();
            for (java.net.HttpCookie c : cs.getCookies())
                if ("camway_session".equals(c.getName())) sessionToken = c.getValue();
        } catch (Exception ignore) {}
    }

    private void loadWall() {
        new Thread(() -> {
            try {
                final List<Tile> got = fetchTilesBlocking();
                runOnUiThread(() -> {
                    if (got.isEmpty()) {
                        hideSplash(0);
                        Toast.makeText(this, "Sin cámaras para este usuario. Mantén Atrás para configurar.",
                                Toast.LENGTH_LONG).show();
                    } else { buildGrid(got); hideSplash(900); }
                });
            } catch (Exception e) {
                final String m = e.getMessage();
                runOnUiThread(() -> {
                    hideSplash(0);
                    Toast.makeText(this,
                        "No se pudo cargar (" + m + "). Mantén Atrás para configurar.",
                        Toast.LENGTH_LONG).show();
                });
            }
        }).start();
    }

    /** Firma del conjunto de cámaras (URLs ordenadas): cambia si el admin reasigna. */
    private String sigOf(List<Tile> list) {
        ArrayList<String> urls = new ArrayList<>();
        for (Tile t : list) urls.add(t.rtsp);
        java.util.Collections.sort(urls);
        StringBuilder sb = new StringBuilder();
        for (String u : urls) sb.append(u).append('|');
        return sb.toString();
    }

    /** Sondeo: si la lista de cámaras cambió (reasignación del admin), reconstruye. */
    private void checkForChanges() {
        if (maxed >= 0 || panel != null || recPlaying) return;   // no reconstruir con overlay/máx
        new Thread(() -> {
            try {
                final List<Tile> got = fetchTilesBlocking();
                if (!sigOf(got).equals(currentSig) && !got.isEmpty()) {
                    runOnUiThread(() -> { if (maxed < 0) buildGrid(got); });
                }
            } catch (Exception ignore) { /* red intermitente: reintenta al próximo ciclo */ }
        }).start();
    }

    private void startPolling() { poll.removeCallbacks(pollTask); poll.postDelayed(pollTask, POLL_MS); }
    private void stopPolling() { poll.removeCallbacks(pollTask); }

    private void post(String urlStr, String body) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setConnectTimeout(8000); c.setReadTimeout(8000);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        OutputStream os = c.getOutputStream();
        os.write(body.getBytes("UTF-8"));
        os.close();
        c.getResponseCode();   // dispara el envío; la cookie la guarda el CookieManager
        c.disconnect();
    }

    private String get(String urlStr) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setConnectTimeout(8000); c.setReadTimeout(8000);
        BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"));
        StringBuilder sb = new StringBuilder();
        String line;
        while ((line = br.readLine()) != null) sb.append(line);
        br.close();
        c.disconnect();
        return sb.toString();
    }

    // ---------------- rejilla ----------------
    private void buildGrid(List<Tile> list) {
        stopAll();
        grid.removeAllViews();
        tiles.clear();
        tiles.addAll(list);
        int n = tiles.size();
        cols = (int) Math.ceil(Math.sqrt(n));
        if (cols < 1) cols = 1;
        int rows = (int) Math.ceil((double) n / cols);
        grid.setColumnCount(cols);
        grid.setRowCount(rows);

        for (int i = 0; i < n; i++) {
            Tile t = tiles.get(i);
            FrameLayout cell = new FrameLayout(this);
            cell.setBackgroundColor(Color.BLACK);
            VLCVideoLayout view = new VLCVideoLayout(this);
            cell.addView(view, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
            TextView lbl = new TextView(this);
            lbl.setText(t.title);
            lbl.setTextColor(Color.WHITE);
            lbl.setTextSize(16);
            lbl.setShadowLayer(3, 0, 1, Color.BLACK);
            lbl.setPadding(14, 6, 14, 8);
            FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.gravity = Gravity.BOTTOM | Gravity.START;
            cell.addView(lbl, lp);

            int row = i / cols, col = i % cols;
            GridLayout.LayoutParams glp = new GridLayout.LayoutParams(
                    GridLayout.spec(row, 1f), GridLayout.spec(col, 1f));
            glp.width = 0; glp.height = 0;
            glp.setMargins(3, 3, 3, 3);
            grid.addView(cell, glp);

            t.cell = cell;
            t.vout = view;
        }
        loaded = true;
        maxed = -1;
        currentSig = sigOf(tiles);     // firma actual (para detectar reasignaciones)
        grid.setVisibility(View.VISIBLE);
        if (centerLogo != null) centerLogo.setVisibility(n == 4 ? View.VISIBLE : View.GONE);
        startGrid();
        focus = Math.min(focus, n - 1); if (focus < 0) focus = 0;
        setFocus(focus);
    }

    private void startGrid() {
        for (Tile t : tiles) play(t);
        startWatchdog();
    }

    private void restartGrid() {
        for (Tile t : tiles) if (t.vlc == null) play(t);
    }

    // ---------------- muro con libVLC (decodificación avcodec eficiente) ----------------
    // libVLC decodifica por HARDWARE donde puede y cae a su propio decodificador por software
    // (avcodec, muchísimo más eficiente que el de Android) para el resto → mueve 4+ cámaras
    // sin el límite de 2 decodificadores HW del TV. Baja latencia por los flags globales
    // (network-caching=100, drop-late-frames, skip-frames, rtsp-tcp) + los de cada media.
    private void play(Tile t) {
        try {
            MediaPlayer mp = new MediaPlayer(libVLC);
            mp.attachViews(t.vout, null, false, false);
            Media m = new Media(libVLC, Uri.parse(t.rtsp));
            m.setHWDecoderEnabled(true, false);   // HW si cabe; si no, cae a software (avcodec)
            m.addOption(":network-caching=100");
            m.addOption(":live-caching=100");
            m.addOption(":clock-jitter=0");
            m.addOption(":clock-synchro=0");
            m.addOption(":drop-late-frames");
            m.addOption(":skip-frames");
            for (String o : getSharedPreferences("camway", MODE_PRIVATE).getString("vlc_media_opts", "").split(",")) {
                if (!o.trim().isEmpty()) m.addOption(o.trim());   // p.ej. ":avcodec-threads=1"
            }
            mp.setMedia(m);
            m.release();
            mp.setEventListener(new MediaPlayer.EventListener() {
                @Override public void onEvent(MediaPlayer.Event ev) {
                    if (ev.type == MediaPlayer.Event.TimeChanged) { t.lastOkAt = System.currentTimeMillis(); t.failCount = 0; }
                    else if (ev.type == MediaPlayer.Event.EncounteredError) scheduleReconnect(t);
                }
            });
            mp.play();
            t.vlc = mp;
            t.lastOkAt = System.currentTimeMillis();   // periodo de gracia hasta el 1er frame
        } catch (Throwable e) {
            stopPlayer(t.vlc); t.vlc = null;
            scheduleReconnect(t);
        }
    }

    // ---------------- watchdog anti-congelamiento ----------------
    private static final long WD_PERIOD_MS = 4000;    // cada cuánto revisa
    private static final long WD_STALL_MS  = 10000;   // sin avanzar el tiempo => congelada/caída

    private final Runnable watchdog = new Runnable() {
        @Override public void run() {
            if (maxed < 0 && !recPlaying) {
                long now = System.currentTimeMillis();
                for (Tile t : tiles) {
                    if (t.vlc == null) continue;
                    if (now - t.lastOkAt > WD_STALL_MS) { t.lastOkAt = now; scheduleReconnect(t); }
                }
            }
            vlcHandler.postDelayed(this, WD_PERIOD_MS);
        }
    };

    private void startWatchdog() {
        vlcHandler.removeCallbacks(watchdog);
        vlcHandler.postDelayed(watchdog, WD_PERIOD_MS);
    }
    private void stopWatchdog() { vlcHandler.removeCallbacks(watchdog); }

    private void stopExo(ExoPlayer p) {
        if (p == null) return;
        try { p.stop(); } catch (Exception e) {}
        try { p.release(); } catch (Exception e) {}
    }

    private void stopPlayer(MediaPlayer mp) {
        if (mp == null) return;
        try { mp.setEventListener(null); } catch (Exception e) {}
        try { mp.stop(); } catch (Exception e) {}
        try { mp.detachViews(); } catch (Exception e) {}
        try { mp.release(); } catch (Exception e) {}
    }

    private void stopAll() {
        vlcHandler.removeCallbacksAndMessages(null);   // cancela reintentos pendientes
        for (Tile t : tiles) { stopPlayer(t.vlc); t.vlc = null; }
        if (maxExo != null) { stopExo(maxExo); maxExo = null; }
        if (recPlayer != null) { stopPlayer(recPlayer); recPlayer = null; recPlaying = false; }
    }

    private static final long RECONNECT_MS_MAX = 60000;   // tope del backoff

    /** Reintenta una cámara caída/congelada tras unos segundos, si el tile sigue vivo. */
    private void scheduleReconnect(Tile t) {
        t.failCount++;
        long delay = Math.min(RECONNECT_MS * (1L << Math.min(t.failCount - 1, 4)), RECONNECT_MS_MAX);
        vlcHandler.postDelayed(() -> {
            if (maxed >= 0) return;            // hay una maximizada: no tocar el grid
            if (!tiles.contains(t)) return;    // el muro se reconstruyó: tile viejo
            stopPlayer(t.vlc); t.vlc = null;
            play(t);
        }, delay);
    }

    private final GradientDrawable border = buildBorder();
    private GradientDrawable buildBorder() {
        GradientDrawable g = new GradientDrawable();
        g.setColor(Color.TRANSPARENT);
        g.setStroke(6, 0xFF2F6FED);
        return g;
    }
    private void setFocus(int i) {
        for (int k = 0; k < tiles.size(); k++) {
            FrameLayout cell = tiles.get(k).cell;
            if (cell != null) cell.setForeground(k == i ? border : null);
        }
        focus = i;
    }

    // ---------------- maximizar / restaurar ----------------
    @OptIn(markerClass = UnstableApi.class)
    private void maximize(int i) {
        if (i < 0 || i >= tiles.size()) return;
        stopAll();                       // libera los decodificadores del muro
        if (maxExo != null) { stopExo(maxExo); maxExo = null; }
        grid.setVisibility(View.GONE);
        if (centerLogo != null) centerLogo.setVisibility(View.GONE);
        maxed = i;
        if (maxView == null) {
            maxView = new TextureView(this);
            root.addView(maxView, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }
        maxView.setVisibility(View.VISIBLE);
        final int which = i;
        Tile t = tiles.get(i);
        try {
            DefaultLoadControl lc = new DefaultLoadControl.Builder()
                    .setBufferDurationsMs(0, 400, 0, 0)
                    .setPrioritizeTimeOverSizeThresholds(true).build();
            DefaultRenderersFactory rf = new DefaultRenderersFactory(this).setEnableDecoderFallback(true);
            maxExo = new ExoPlayer.Builder(this, rf).setLoadControl(lc).build();
            maxExo.setVideoTextureView(maxView);
            maxExo.addListener(new Player.Listener() {
                @Override public void onPlayerError(PlaybackException error) {
                    if (maxed == which) vlcHandler.postDelayed(() -> { if (maxed == which) maximize(which); }, RECONNECT_MS);
                }
            });
            RtspMediaSource ms = new RtspMediaSource.Factory().setForceUseRtpTcp(true)
                    .createMediaSource(MediaItem.fromUri(Uri.parse(t.rtspHi)));
            maxExo.setMediaSource(ms);
            maxExo.prepare();
            maxExo.setPlayWhenReady(true);
        } catch (Exception e) {}
    }
    private void restore() {
        if (maxExo != null) { stopExo(maxExo); maxExo = null; }
        if (maxView != null) maxView.setVisibility(View.GONE);
        // Se reconstruye el muro con vistas y reproductores NUEVOS: reusar los VLCVideoLayout que
        // estuvieron ocultos (grid GONE) deja la superficie de vídeo muerta y las celdas congeladas.
        buildGrid(new ArrayList<>(tiles));
    }

    // ---------------- menú + grabaciones (overlay) ----------------
    private interface OnPick { void pick(int index); }
    private Tile recCam;

    private static String enc(String s) {
        try { return java.net.URLEncoder.encode(s == null ? "" : s, "UTF-8"); }
        catch (Exception e) { return ""; }
    }

    private void closePanelView() {
        if (panel != null) { root.removeView(panel); panel = null; onPanelBack = null; }
    }
    private void closePanel() { closePanelView(); }   // vuelve al muro (sigue vivo detrás)

    /** Lista navegable (menú o grabaciones) como overlay a la izquierda. */
    private void showListPanel(String title, List<String> labels, OnPick onPick, Runnable onBack) {
        closePanelView();
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackgroundColor(0xF005070C);

        TextView tv = new TextView(this);
        tv.setText(title); tv.setTextColor(0xFF8AA0C0); tv.setTextSize(22);
        tv.setPadding(36, 30, 36, 16);
        box.addView(tv);

        ScrollView sv = new ScrollView(this);
        LinearLayout list = new LinearLayout(this);
        list.setOrientation(LinearLayout.VERTICAL);
        sv.addView(list);
        box.addView(sv, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        for (int i = 0; i < labels.size(); i++) {
            final int idx = i;
            Button b = new Button(this);
            b.setText(labels.get(i)); b.setAllCaps(false);
            b.setTextColor(Color.WHITE); b.setTextSize(18);
            b.setBackgroundColor(Color.TRANSPARENT);
            b.setPadding(36, 22, 36, 22);
            b.setGravity(Gravity.CENTER_VERTICAL | Gravity.START);
            b.setOnFocusChangeListener((v, has) -> v.setBackgroundColor(has ? 0xFF2F6FED : Color.TRANSPARENT));
            b.setOnClickListener(v -> onPick.pick(idx));
            list.addView(b, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        }

        int w = (int) (getResources().getDisplayMetrics().widthPixels * 0.62);
        FrameLayout.LayoutParams blp = new FrameLayout.LayoutParams(
                w, ViewGroup.LayoutParams.MATCH_PARENT);
        blp.gravity = Gravity.START;
        root.addView(box, blp);
        panel = box;
        onPanelBack = onBack;
        if (list.getChildCount() > 0) list.getChildAt(0).requestFocus();
    }

    private void showMenu() {
        if (maxed >= 0) restore();
        List<String> items = new ArrayList<>();
        items.add("▶  En vivo");
        items.add("⏺  Grabaciones");
        items.add("⚙  Ajustes");
        items.add("🚪  Salir de la app");
        showListPanel("Menú", items, idx -> {
            if (idx == 0) closePanel();
            else if (idx == 1) showRecCameras();
            else if (idx == 2) { closePanel(); startActivity(new Intent(this, SetupActivity.class)); }
            else {                        // Salir → cerrar la app e ir al inicio del TV
                closePanel();
                stopAll();
                finishAndRemoveTask();
            }
        }, this::closePanel);
    }

    private void showRecCameras() {
        List<String> names = new ArrayList<>();
        for (Tile t : tiles) names.add(t.title);
        if (names.isEmpty()) { Toast.makeText(this, "No hay cámaras", Toast.LENGTH_SHORT).show(); return; }
        showListPanel("Grabaciones — elige cámara", names,
                idx -> showRecClips(tiles.get(idx)), this::showMenu);
    }

    private void showRecClips(final Tile cam) {
        new Thread(() -> {
            List<Rec> recs;
            try { recs = fetchRecsBlocking(cam.deviceId); }
            catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(this, "No se pudo listar: " + e.getMessage(),
                        Toast.LENGTH_LONG).show());
                return;
            }
            final List<Rec> frecs = recs;
            runOnUiThread(() -> {
                if (frecs.isEmpty()) {
                    Toast.makeText(this, "Sin grabaciones para " + cam.title, Toast.LENGTH_LONG).show();
                    return;
                }
                List<String> labels = new ArrayList<>();
                for (Rec r : frecs) labels.add(fmtRec(r));
                showListPanel("Grabaciones — " + cam.title, labels,
                        idx -> playRecording(cam, frecs.get(idx)), this::showRecCameras);
            });
        }).start();
    }

    private List<Rec> fetchRecsBlocking(String deviceId) throws Exception {
        String json = get(baseUrl + "/api/recordings/" + deviceId);
        JSONArray arr = new JSONObject(json).getJSONArray("recordings");
        List<Rec> out = new ArrayList<>();
        for (int i = 0; i < arr.length(); i++) {
            JSONObject o = arr.getJSONObject(i);
            Rec r = new Rec();
            r.name = o.optString("name"); r.stream = o.optString("stream");
            r.kind = o.optString("kind", "cont"); r.size = o.optLong("size");
            r.mtime = o.optDouble("mtime");
            out.add(r);
            if (out.size() >= 200) break;
        }
        return out;
    }

    private String fmtRec(Rec r) {
        CharSequence when = android.text.format.DateFormat.format("dd/MM HH:mm:ss", (long) (r.mtime * 1000));
        String tag = "event".equals(r.kind) ? "  🔔 evento" : "";
        return when + tag + "    ·  " + (r.size / 1048576) + " MB";
    }

    private void playRecording(Tile cam, Rec r) {
        closePanelView();
        stopAll();
        grid.setVisibility(View.GONE);
        recCam = cam;
        if (maxVlc == null) {
            maxVlc = new VLCVideoLayout(this);
            root.addView(maxVlc, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }
        maxVlc.setVisibility(View.VISIBLE);
        String url = baseUrl + "/api/recordings/" + cam.deviceId + "/file?stream=" + enc(r.stream)
                + "&name=" + enc(r.name) + "&kind=" + r.kind + "&token=" + enc(sessionToken);
        try {
            recPlayer = new MediaPlayer(libVLC);
            recPlayer.attachViews(maxVlc, null, false, false);
            Media m = new Media(libVLC, Uri.parse(url));
            m.setHWDecoderEnabled(true, false);
            recPlayer.setMedia(m); m.release();
            recPlayer.play();
            recPlaying = true;
        } catch (Exception e) {
            Toast.makeText(this, "No se pudo reproducir", Toast.LENGTH_LONG).show();
            backFromRecording();
        }
    }

    private void stopRecPlayer() {
        if (recPlayer != null) { stopPlayer(recPlayer); recPlayer = null; }
        if (maxVlc != null) maxVlc.setVisibility(View.GONE);
        recPlaying = false;
    }

    private void backFromRecording() {
        stopRecPlayer();
        if (recCam != null) showRecClips(recCam);   // vuelve a la lista de clips
        else { grid.setVisibility(View.VISIBLE); restartGrid(); }
    }

    // ---------------- navegación con control ----------------
    private void move(int dc, int dr) {
        int r = focus / cols, c = focus % cols;
        c += dc; r += dr;
        int rows = (int) Math.ceil((double) tiles.size() / cols);
        if (c < 0) c = 0; if (c >= cols) c = cols - 1;
        if (r < 0) r = 0; if (r >= rows) r = rows - 1;
        int idx = r * cols + c;
        if (idx >= tiles.size()) idx = tiles.size() - 1;
        setFocus(idx);
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent e) {
        if (keyCode == KeyEvent.KEYCODE_MENU || keyCode == KeyEvent.KEYCODE_SETTINGS) {
            showMenu();
            return true;
        }
        if (keyCode == KeyEvent.KEYCODE_BACK) { e.startTracking(); return true; }
        // Con overlay (menú/grabaciones) o reproduciendo, deja que Android maneje
        // el foco (flechas/OK sobre los botones de la lista).
        if (panel != null || recPlaying) return super.onKeyDown(keyCode, e);
        if (!tiles.isEmpty() && maxed < 0) {
            switch (keyCode) {
                case KeyEvent.KEYCODE_DPAD_LEFT:  move(-1, 0); return true;
                case KeyEvent.KEYCODE_DPAD_RIGHT: move(1, 0); return true;
                case KeyEvent.KEYCODE_DPAD_UP:    move(0, -1); return true;
                case KeyEvent.KEYCODE_DPAD_DOWN:  move(0, 1); return true;
                case KeyEvent.KEYCODE_DPAD_CENTER:
                case KeyEvent.KEYCODE_ENTER:      maximize(focus); return true;
            }
        }
        return super.onKeyDown(keyCode, e);
    }

    @Override
    public boolean onKeyLongPress(int keyCode, KeyEvent e) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            showMenu();           // Atrás MANTENIDO = abrir el menú
            return true;
        }
        return super.onKeyLongPress(keyCode, e);
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent e) {
        if (keyCode == KeyEvent.KEYCODE_BACK && !e.isCanceled() && !e.isLongPress()) {
            if (recPlaying) backFromRecording();
            else if (panel != null) { if (onPanelBack != null) onPanelBack.run(); else closePanel(); }
            else if (maxed >= 0) restore();
            // En la pantalla raiz del muro, Atrás NO debe hacer nada: antes
            // mandaba la app a segundo plano (moveTaskToBack) y nada la traia
            // de vuelta — un BACK accidental (control con el boton pegado,
            // alguien de paso) dejaba la TV en el launcher indefinidamente.
            // La salida real sigue siendo Atrás MANTENIDO -> menu.
            return true;
        }
        return super.onKeyUp(keyCode, e);
    }

    @Override
    protected void onDestroy() {
        stopAll();
        if (libVLC != null) { try { libVLC.release(); } catch (Exception e) {} }
        super.onDestroy();
    }

    private void immersive() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }
}
