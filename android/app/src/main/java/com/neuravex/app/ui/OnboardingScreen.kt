package com.neuravex.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Divider
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import com.neuravex.app.data.ApiClientFactory
import com.neuravex.app.data.AppConfigStore
import com.neuravex.app.security.BiometricGate
import com.neuravex.app.security.SecureCredentialStore
import kotlinx.coroutines.launch

/**
 * Three things happen here, in order:
 * 1. Backend connection (self-hosted NEURAVEX server URL).
 * 2. Login (email + password against your NEURAVEX account). The JWT
 *    this returns is stored via SecureCredentialStore and attached
 *    automatically to every future request by ApiClientFactory's
 *    interceptor.
 * 3. Exchange API key/secret — optional at onboarding time.
 */
@Composable
fun OnboardingScreen(configStore: AppConfigStore, onDone: () -> Unit) {
    var backendUrl by remember { mutableStateOf(configStore.backendBaseUrl ?: "https://") }
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var accountId by remember { mutableStateOf(configStore.accountId ?: "") }
    // (apiKey/apiSecret removed along with the exchange-key onboarding
    // step — see the wallet-only change below.)
    var statusMessage by remember { mutableStateOf<String?>(null) }
    var isLoggingIn by remember { mutableStateOf(false) }
    var loggedIn by remember { mutableStateOf(false) }

    val context = LocalContext.current
    val credentialStore = remember { SecureCredentialStore(context) }
    val scope = rememberCoroutineScope()

    Column(
        modifier = Modifier.fillMaxSize().padding(24.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("NEURAVEX", color = NeuravexColors.Silver, style = androidx.compose.material3.MaterialTheme.typography.headlineMedium)
        Text("AI Trading. Beyond Human.", color = NeuravexColors.VioletGlow)

        Divider()
        Text("1. Connect to your NEURAVEX server", color = NeuravexColors.Silver)
        OutlinedTextField(
            value = backendUrl, onValueChange = { backendUrl = it; loggedIn = false },
            label = { Text("Backend URL") }, modifier = Modifier.fillMaxWidth(),
            enabled = !loggedIn,
        )

        Divider()
        Text("2. Log in", color = NeuravexColors.Silver)
        OutlinedTextField(
            value = email, onValueChange = { email = it },
            label = { Text("Email") }, modifier = Modifier.fillMaxWidth(),
            enabled = !loggedIn,
        )
        OutlinedTextField(
            value = password, onValueChange = { password = it },
            label = { Text("Password") }, modifier = Modifier.fillMaxWidth(),
            visualTransformation = PasswordVisualTransformation(),
            enabled = !loggedIn,
        )

        if (loggedIn) {
            Text("Logged in", color = NeuravexColors.Profit)
        } else {
            Button(
                onClick = {
                    if (backendUrl.isBlank() || email.isBlank() || password.isBlank()) {
                        statusMessage = "Backend URL, email and password are required"
                        return@Button
                    }
                    isLoggingIn = true
                    statusMessage = null
                    scope.launch {
                        try {
                            val api = ApiClientFactory.create(backendUrl.trimEnd('/'), credentialStore)
                            val response = api.login(email, password)
                            if (response.isSuccessful && response.body() != null) {
                                credentialStore.saveBackendToken(response.body()!!.access_token)
                                configStore.backendBaseUrl = backendUrl.trimEnd('/')
                                loggedIn = true
                                statusMessage = null
                            } else {
                                statusMessage = "Login failed (HTTP ${response.code()}) — check your email/password"
                            }
                        } catch (e: Exception) {
                            statusMessage = "Could not reach the server — check the backend URL and that it's running"
                        }
                        isLoggingIn = false
                    }
                },
                modifier = Modifier.fillMaxWidth(),
                enabled = !isLoggingIn,
            ) {
                if (isLoggingIn) {
                    CircularProgressIndicator(modifier = Modifier.padding(2.dp))
                } else {
                    Text("Log in")
                }
            }
        }

        if (loggedIn) {
            Divider()
            Text("3. Your account", color = NeuravexColors.Silver)
            OutlinedTextField(
                value = accountId, onValueChange = { accountId = it },
                label = { Text("Account ID") }, modifier = Modifier.fillMaxWidth(),
            )

            statusMessage?.let { Text(it, color = NeuravexColors.Loss) }

            // The exchange API key step (Binance) that used to live here
            // has been removed — NEURAVEX now only ever trades through
            // the wallet key stored on-device (see the safety
            // architecture, and the same change in Settings). Onboarding
            // no longer offers a second, less-safe key type; the wallet
            // key is entered later, in Settings, after this step.
            Button(
                onClick = {
                    if (accountId.isBlank()) {
                        statusMessage = "Account ID is required"
                        return@Button
                    }
                    configStore.accountId = accountId
                    onDone()
                },
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("Continue")
            }
        } else {
            statusMessage?.let { Text(it, color = NeuravexColors.Loss) }
        }
    }
}
