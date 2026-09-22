package com.neuravex.app

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import com.neuravex.app.data.AppConfigStore
import com.neuravex.app.ui.DashboardScreen
import com.neuravex.app.ui.NeuravexColors
import com.neuravex.app.ui.OnboardingScreen
import com.neuravex.app.ui.SettingsScreen
import com.neuravex.app.ui.theme.NeuravexTheme

// FragmentActivity, not the plain androidx.activity.ComponentActivity —
// this is load-bearing, not stylistic. BiometricGate.kt requires a
// FragmentActivity to host the biometric prompt UI, and every screen that
// uses it (OnboardingScreen, SettingsScreen) does `context as?
// FragmentActivity`. androidx.activity.ComponentActivity and
// androidx.fragment.app.FragmentActivity are SIBLING classes, not
// parent/child, despite the similar names — a plain ComponentActivity is
// never a FragmentActivity, so that cast would silently return null and
// every biometric-gated action (saving a wallet key, saving an exchange
// key, enabling AI trading) would silently no-op, with the code never
// even reaching BiometricGate, let alone its own error handling.
// FragmentActivity itself extends ComponentActivity, so Compose's
// setContent { } below still works exactly the same either way.
class MainActivity : FragmentActivity() {

    // Android 13+ (API 33+) requires the POST_NOTIFICATIONS runtime
    // permission before ANY notification can be shown — including the
    // one TradingExecutionService's foreground service depends on to stay
    // alive. Declaring the permission in AndroidManifest.xml alone (which
    // this app already did) is NOT enough on API 33+; without this
    // runtime request, the service can start but its notification never
    // appears, and on several OEM skins (OnePlus/OxygenOS included) a
    // foreground service with no visible notification gets killed almost
    // immediately, silently, with nothing in the UI to explain why.
    private val notificationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { /* Whether granted or denied, we don't block app usage on it —
         see the explanation this triggers below for what denying means. */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        requestNotificationPermissionIfNeeded()
        setContent {
            NeuravexTheme {
                Surface(color = NeuravexColors.Void) {
                    NeuravexNavHost()
                }
            }
        }
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return // not needed before Android 13
        val alreadyGranted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS,
        ) == PackageManager.PERMISSION_GRANTED
        if (!alreadyGranted) {
            notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
}

private enum class Screen { Onboarding, Dashboard, Settings }

@Composable
private fun NeuravexNavHost() {
    val context = androidx.compose.ui.platform.LocalContext.current
    val configStore = remember {
        AppConfigStore(context).also {
            // The registry holds no state across process restarts — this
            // is the one place that re-applies the user's persisted
            // custom token (if any) so PolygonTokenRegistry.ALL includes
            // it from the very first frame, before Settings or Dashboard
            // ever read from it.
            it.applyCustomTokenToRegistry()
        }
    }
    var screen by remember { mutableStateOf(if (configStore.isOnboarded()) Screen.Dashboard else Screen.Onboarding) }

    when (screen) {
        Screen.Onboarding -> OnboardingScreen(
            configStore = configStore,
            onDone = { screen = Screen.Dashboard },
        )
        Screen.Dashboard -> DashboardScreen(
            configStore = configStore,
            onOpenSettings = { screen = Screen.Settings },
        )
        Screen.Settings -> SettingsScreen(
            configStore = configStore,
            onBack = { screen = Screen.Dashboard },
        )
    }
}
