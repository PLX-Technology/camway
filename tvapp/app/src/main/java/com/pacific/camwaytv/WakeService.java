package com.pacific.camwaytv;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.SystemClock;
import android.provider.Settings;

/**
 * Abre CamWay TV cuando el televisor despierta de la espera. El botón de encendido
 * del control no reinicia el TV, así que BOOT_COMPLETED no se emite; en su lugar
 * escuchamos que la pantalla se encienda. Necesita el permiso "Mostrar sobre otras apps".
 */
public class WakeService extends Service {
    private static final String CH = "camway_wake";
    private long lastLaunch = 0;

    private final BroadcastReceiver wake = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) {
            long now = SystemClock.elapsedRealtime();
            if (now - lastLaunch < 5000) return;     // varias señales seguidas: una sola vez
            lastLaunch = now;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(c)) return;
            Intent a = new Intent(c, MainActivity.class);
            a.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try { c.startActivity(a); } catch (Exception ignore) {}
        }
    };

    /** Arranca el servicio si se puede; seguro de llamar varias veces. */
    public static void start(Context c) {
        try {
            Intent i = new Intent(c, WakeService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) c.startForegroundService(i);
            else c.startService(i);
        } catch (Exception ignore) {}
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
            nm.createNotificationChannel(new NotificationChannel(CH, "CamWay TV",
                    NotificationManager.IMPORTANCE_MIN));
        Notification.Builder nb = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CH) : new Notification.Builder(this);
        Notification n = nb.setContentTitle("CamWay TV")
                .setContentText("Abre las cámaras al encender el televisor")
                .setSmallIcon(android.R.drawable.ic_menu_camera)
                .setOngoing(true).build();
        if (Build.VERSION.SDK_INT >= 34)
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(1, n);

        IntentFilter f = new IntentFilter();
        f.addAction(Intent.ACTION_SCREEN_ON);
        f.addAction(Intent.ACTION_USER_PRESENT);
        f.addAction("android.intent.action.DREAMING_STOPPED");
        registerReceiver(wake, f);
    }

    @Override public int onStartCommand(Intent i, int flags, int id) { return START_STICKY; }
    @Override public IBinder onBind(Intent i) { return null; }

    @Override
    public void onDestroy() {
        try { unregisterReceiver(wake); } catch (Exception ignore) {}
        super.onDestroy();
    }
}
