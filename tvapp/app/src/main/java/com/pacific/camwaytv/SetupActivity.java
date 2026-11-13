package com.pacific.camwaytv;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.RadioGroup;
import android.widget.Toast;

/** Ajuste inicial: IP, puerto, usuario y clave del servidor CamWay. */
public class SetupActivity extends Activity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_setup);

        final SharedPreferences p = getSharedPreferences("camway", MODE_PRIVATE);
        final EditText ip = findViewById(R.id.ip);
        final EditText port = findViewById(R.id.port);
        final EditText user = findViewById(R.id.user);
        final EditText pass = findViewById(R.id.pass);
        Button save = findViewById(R.id.save);
        Button autostart = findViewById(R.id.autostart);
        final RadioGroup sourceGroup = findViewById(R.id.sourceGroup);
        Button manualBtn = findViewById(R.id.manualBtn);

        // Fuente actual (Servidor por defecto).
        if ("manual".equals(p.getString("source", "server"))) {
            sourceGroup.check(R.id.sourceManual);
        } else {
            sourceGroup.check(R.id.sourceServer);
        }
        sourceGroup.setOnCheckedChangeListener((g, id) ->
                p.edit().putString("source", id == R.id.sourceManual ? "manual" : "server").apply());

        manualBtn.setOnClickListener(v ->
                startActivity(new Intent(SetupActivity.this, CamerasActivity.class)));

        refreshAutostartLabel(autostart);
        autostart.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                if (canAutostart()) {
                    Toast.makeText(SetupActivity.this,
                            "El inicio automático ya está activo", Toast.LENGTH_SHORT).show();
                    return;
                }
                try {
                    Intent i = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                            Uri.parse("package:" + getPackageName()));
                    startActivity(i);
                } catch (Exception e) {
                    try { startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION)); }
                    catch (Exception e2) {
                        Toast.makeText(SetupActivity.this, "Ajustes › Apps › Acceso especial › "
                                + "Mostrar sobre otras apps › CamWay TV", Toast.LENGTH_LONG).show();
                    }
                }
            }
        });

        ip.setText(p.getString("ip", ""));
        port.setText(p.getString("port", "8120"));
        user.setText(p.getString("user", ""));
        pass.setText(p.getString("pass", ""));
        ip.requestFocus();

        save.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                String ipv = ip.getText().toString().trim();
                String portv = port.getText().toString().trim();
                String userv = user.getText().toString().trim();
                String passv = pass.getText().toString();
                boolean manual = sourceGroup.getCheckedRadioButtonId() == R.id.sourceManual;
                if (!manual && ipv.isEmpty()) {
                    Toast.makeText(SetupActivity.this, "Escribe la IP del servidor (o elige Cámaras manuales)", Toast.LENGTH_LONG).show();
                    return;
                }
                if (portv.isEmpty()) portv = "8120";
                p.edit().putString("ip", ipv).putString("port", portv)
                        .putString("user", userv).putString("pass", passv).apply();
                Toast.makeText(SetupActivity.this, "Guardado", Toast.LENGTH_SHORT).show();
                finish();
            }
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        Button autostart = findViewById(R.id.autostart);
        if (autostart != null) refreshAutostartLabel(autostart);
    }

    private boolean canAutostart() {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(this);
    }

    private void refreshAutostartLabel(Button b) {
        b.setText(canAutostart()
                ? "Inicio automático: ACTIVADO ✓"
                : "Inicio automático: tocar para activar");
    }
}
