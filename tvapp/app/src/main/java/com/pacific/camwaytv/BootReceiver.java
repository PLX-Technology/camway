package com.pacific.camwaytv;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Lanza CamWay TV automáticamente al encender el televisor. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        String a = intent != null ? intent.getAction() : null;
        if (a == null) return;
        if (a.equals(Intent.ACTION_BOOT_COMPLETED)
                || a.equals("android.intent.action.LOCKED_BOOT_COMPLETED")
                || a.equals("android.intent.action.QUICKBOOT_POWERON")
                || a.equals("com.htc.intent.action.QUICKBOOT_POWERON")) {
            Intent i = new Intent(ctx, MainActivity.class);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try { ctx.startActivity(i); } catch (Exception ignore) {}
            WakeService.start(ctx);
        }
    }
}
