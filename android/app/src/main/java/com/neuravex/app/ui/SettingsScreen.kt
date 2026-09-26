package com.neuravex.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.Divider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import com.neuravex.app.data.AccountSettingsUpdate
import com.neuravex.app.data.ApiClientFactory
import com.neuravex.app.data.AppConfigStore
import com.neuravex.app.data.CurrencyConverter
import com.neuravex.app.data.SupportedCoins
import com.neuravex.app.data.SupportedPolygonCoins
import com.neuravex.app.security.BiometricGate
import com.neuravex.app.security.SecureCredentialStore
import kotlinx.coroutines.launch

/**
 * Deliberately minimal (spec: "ik doe en denk zo weinig mogelijk"). Four
 * things only:
 *   1. Which venue: exchange (Binance) or wallet (Polygon)
 *   2. Private key / API credentials for that venue (one-time entry, with
 *      the option to change or remove it later — see the "Change key" /
 *      "Remove key" actions below)
 *   3. Which coins the AI may trade
 *   4. One absolute stop amount, and the big on/off switch
 *
 * The whole screen is ONE LazyColumn (not a plain Column with a nested
 * LazyColumn for the coin checkboxes) — nesting a scrollable list inside
 * another scrollable container is a common Compose crash/layout bug.
 */
@Composable
fun SettingsScreen(configStore: AppConfigStore, onBack: () -> Unit) {
    val context = LocalContext.current
    val credentialStore = remember { SecureCredentialStore(context) }
    val scope = rememberCoroutineScope()
    val api = remember { ApiClientFactory.create(configStore.backendBaseUrl!!, credentialStore) }
    val accountId = configStore.accountId!!

    var isLoading by remember { mutableStateOf(true) }
    var statusMessage by remember { mutableStateOf<String?>(null) }

    var venue by remember { mutableStateOf("wallet") } // "wallet" | "cex"
    var selectedCoins by remember { mutableStateOf(setOf<String>()) }
    var balanceFloorText by remember { mutableStateOf("") }
    var profitTargetText by remember { mutableStateOf("") }
    var aiEnabled by remember { mutableStateOf(false) }
    var paperTradingEnabled by remember { mutableStateOf(true) }
    var paperStartingBalanceText by remember { mutableStateOf("1000") }
    var paperEquity by remember { mutableStateOf(1000.0) }
    // BUG FIX: the backend's paper_starting_balance / paper_equity are
    // USD-equivalent (paper_balances is literally seeded as {"USDC": ...}
    // — see settings.py) — the exact same "raw dollar number shown with a
    // € sign" trap CurrencyConverter's own doc describes, which the
    // Dashboard already avoids for the real wallet (DashboardScreen.kt)
    // but this screen's simulation fields never did. That's why resetting
    // the simulation to a typed amount always seemed to "go to a
    // different number" the moment the Dashboard (which DOES convert)
    // showed it back: e.g. typing 1000 sent $1000 to the backend, and the
    // Dashboard then correctly showed ~€877 of it — nothing was wrong on
    // the backend, the two screens just disagreed about which currency
    // this field was in. Converted consistently with the Dashboard below.
    var usdToEurRate by remember { mutableStateOf<Double?>(null) }
    var customSymbolText by remember { mutableStateOf(configStore.customTokenSymbol ?: "") }
    var customAddressText by remember { mutableStateOf(configStore.customTokenAddress ?: "") }
    var customDecimalsText by remember { mutableStateOf(configStore.customTokenDecimals.toString()) }
    // Previously the only way to change networks was uninstalling and
    // reinstalling the app (the backend URL was set once during
    // onboarding and never exposed again). Editable here now — the app
    // still needs a restart to actually pick up a new URL, since the API
    // client is built once per app launch, but that's a restart instead
    // of a full reinstall.
    var backendUrlText by remember { mutableStateOf(configStore.backendBaseUrl ?: "") }

    var apiKey by remember { mutableStateOf("") }
    var apiSecret by remember { mutableStateOf("") }
    var hasExchangeKey by remember { mutableStateOf(false) }
    var walletPrivateKey by remember { mutableStateOf("") }
    var hasWalletKey by remember { mutableStateOf(false) }
    var walletAddress by remember { mutableStateOf<String?>(null) }
    // When true, shows the entry fields again even though a key is
    // already saved — this is the "Change key" flow: the old key is
    // cleared first, then the normal "enter + save" fields reappear.
    var isReplacingKey by remember { mutableStateOf(false) }

    fun persist(update: AccountSettingsUpdate, onDone: (Boolean, String?) -> Unit) {
        scope.launch {
            try {
                val response = api.updateSettings(accountId, update)
                if (response.isSuccessful) {
                    onDone(true, null)
                } else {
                    onDone(false, "Could not save (HTTP ${response.code()}): ${response.errorBody()?.string()}")
                }
            } catch (e: Exception) {
                onDone(false, "Could not save — check your connection")
            }
        }
    }

    LaunchedEffect(Unit) {
        hasExchangeKey = credentialStore.hasCredentials()
        hasWalletKey = credentialStore.hasWalletKey()
        if (hasWalletKey) {
            walletAddress = credentialStore.getWalletAddress()
        }
        // Fetched before the settings response is handled below, so the
        // USD -> EUR conversion of paper_starting_balance/paper_equity has
        // a real rate to use the first time this screen renders them.
        val rate = CurrencyConverter.getUsdToEurRate()
        usdToEurRate = rate
        try {
            val response = api.getSettings(accountId)
            if (response.isSuccessful) {
                response.body()?.let { s ->
                    venue = s.execution_venue
                    if (venue == "cex") {
                        // NEURAVEX only ever executes through the wallet
                        // key stored on this device — there is no
                        // server-side exchange-credential path (see the
                        // safety architecture), so an account still set
                        // to "cex" from an older build is silently
                        // normalized to "wallet" the first time this
                        // screen loads, rather than exposing a choice
                        // that would need a second, less-safe key type.
                        venue = "wallet"
                        selectedCoins = emptySet()
                        persist(AccountSettingsUpdate(execution_venue = "wallet", symbols = emptyList())) { _, _ -> }
                    }
                    selectedCoins = s.symbols.toSet()
                    balanceFloorText = s.min_balance_floor?.let { "%.0f".format(it) } ?: ""
                    profitTargetText = s.max_balance_target?.let { "%.0f".format(it) } ?: ""
                    aiEnabled = s.ai_enabled
                    paperTradingEnabled = s.paper_trading_enabled
                    // Both are USD from the backend — see the field
                    // comment above for why these are converted to EUR
                    // here, matching how the Dashboard shows this same
                    // number.
                    paperStartingBalanceText = "%.0f".format(s.paper_starting_balance * rate)
                    paperEquity = s.paper_equity * rate
                }
            } else {
                statusMessage = "Could not load settings: HTTP ${response.code()}"
            }
        } catch (e: Exception) {
            statusMessage = "Could not load settings: ${e.javaClass.simpleName}: ${e.message}"
        }
        isLoading = false
    }

    val coinOptions = if (venue == "wallet") SupportedPolygonCoins.ALL else SupportedCoins.ALL
    val hasCredential = if (venue == "wallet") hasWalletKey else hasExchangeKey

    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        item {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("Settings", style = MaterialTheme.typography.headlineSmall, color = NeuravexColors.Silver)
                TextButton(onClick = onBack) { Text("Done") }
            }
        }

        if (isLoading) {
            item { Text("Loading…", color = NeuravexColors.SilverDim) }
            return@LazyColumn
        }

        // --- 0. Backend server — editable here now instead of requiring
        // an app reinstall to switch networks.
        item {
            Column {
                Text("NEURAVEX server", color = NeuravexColors.Silver)
                OutlinedTextField(
                    value = backendUrlText, onValueChange = { backendUrlText = it.trim() },
                    label = { Text("Backend URL") }, modifier = Modifier.fillMaxWidth(), singleLine = true,
                )
                TextButton(onClick = {
                    if (backendUrlText.isBlank() || !backendUrlText.startsWith("http")) {
                        statusMessage = "Enter a valid URL, e.g. http://192.168.1.10:8000"
                        return@TextButton
                    }
                    configStore.backendBaseUrl = backendUrlText
                    statusMessage = "Saved — close and reopen the app for it to take effect"
                }) { Text("Save server address") }
            }
        }

        item { Divider() }

        // --- 1. Venue — wallet only. Not user-selectable: NEURAVEX only
        // ever executes through the wallet key stored on this device
        // (see the safety architecture) — there is no server-side
        // exchange-credential path, so a CEX option here would be a
        // second, less-safe way in that most users would never need. If
        // this account was ever set to "cex" from an older build, it's
        // silently normalized to "wallet" the first time this screen is
        // opened (normalized once, right when settings are first loaded
        // — see LaunchedEffect(Unit) near the top of this function; that
        // is the composable context this normalization actually has to
        // run in, not here inside the LazyColumn item lambda).
        item {
            Text("Trading via your Polygon wallet", color = NeuravexColors.Silver)
        }

        item { Divider() }

        // --- 2. One-time credential entry, with change/remove ---
        item {
            if (hasCredential && !isReplacingKey) {
                Column {
                    Text(
                        if (venue == "wallet") "Wallet key saved on this device" else "Exchange key saved on this device",
                        color = NeuravexColors.Profit,
                    )
                    if (venue == "wallet") {
                        // Purely local key derivation, no network call — safe
                        // to show, since a wallet address is meant to be
                        // public. Compare this against MetaMask's "Account 1"
                        // address to confirm the saved key really controls
                        // the wallet you expect it to.
                        Text(
                            walletAddress?.let { "Address: $it" } ?: "Address: (could not derive — key may be invalid)",
                            color = NeuravexColors.SilverDim,
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        TextButton(onClick = {
                            isReplacingKey = true
                            statusMessage = null
                        }) { Text("Change key") }
                        TextButton(onClick = {
                            scope.launch {
                                if (venue == "wallet") {
                                    credentialStore.clearWalletKey()
                                    hasWalletKey = false
                                    walletAddress = null
                                } else {
                                    credentialStore.clearExchangeCredentials()
                                    hasExchangeKey = false
                                }
                                // A key-less venue can no longer auto-execute —
                                // turn AI trading off rather than leaving it
                                // "on" with nothing able to act on it. Same
                                // safety fix as the main Stop button: don't
                                // silently discard a failed save here either.
                                if (aiEnabled) {
                                    persist(AccountSettingsUpdate(ai_enabled = false)) { success, error ->
                                        if (success) {
                                            aiEnabled = false
                                            statusMessage = "Key removed from this device, AI trading stopped"
                                        } else {
                                            statusMessage = "Key removed, but AI trading STOP did not reach the server: $error"
                                        }
                                    }
                                    return@launch
                                }
                                statusMessage = "Key removed from this device"
                            }
                        }) { Text("Remove key", color = NeuravexColors.Loss) }
                    }
                }
            } else if (venue == "wallet") {
                Column {
                    Text("Enter your MetaMask private key (stored only on this device)", color = NeuravexColors.Silver)
                    OutlinedTextField(
                        value = walletPrivateKey, onValueChange = { walletPrivateKey = it },
                        label = { Text("Wallet private key") }, modifier = Modifier.fillMaxWidth(),
                        visualTransformation = PasswordVisualTransformation(),
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        TextButton(onClick = {
                            if (walletPrivateKey.isBlank()) return@TextButton
                            val activity = context as? FragmentActivity ?: return@TextButton
                            BiometricGate(activity).requireConfirmation(
                                title = "Confirm saving wallet key",
                                subtitle = "This key will be encrypted and stored only on this device",
                                onSuccess = {
                                    scope.launch {
                                        credentialStore.saveWalletPrivateKey(walletPrivateKey)
                                        hasWalletKey = true
                                        walletAddress = credentialStore.getWalletAddress()
                                        isReplacingKey = false
                                        walletPrivateKey = ""
                                        statusMessage = null
                                    }
                                },
                                onFailure = { statusMessage = "Not saved — biometric confirmation failed: $it" },
                            )
                        }) { Text("Save wallet key") }
                        if (isReplacingKey) {
                            TextButton(onClick = { isReplacingKey = false; walletPrivateKey = "" }) { Text("Cancel") }
                        }
                    }
                }
            } else {
                Column {
                    Text("Enter a Binance API key (trading permission only)", color = NeuravexColors.Silver)
                    OutlinedTextField(
                        value = apiKey, onValueChange = { apiKey = it },
                        label = { Text("API key") }, modifier = Modifier.fillMaxWidth(),
                    )
                    OutlinedTextField(
                        value = apiSecret, onValueChange = { apiSecret = it },
                        label = { Text("API secret") }, modifier = Modifier.fillMaxWidth(),
                        visualTransformation = PasswordVisualTransformation(),
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        TextButton(onClick = {
                            if (apiKey.isBlank() || apiSecret.isBlank()) return@TextButton
                            val activity = context as? FragmentActivity ?: return@TextButton
                            BiometricGate(activity).requireConfirmation(
                                title = "Confirm saving exchange key",
                                subtitle = "This key will be encrypted and stored only on this device",
                                onSuccess = {
                                    scope.launch {
                                        credentialStore.saveExchangeCredentials(apiKey, apiSecret)
                                        hasExchangeKey = true
                                        isReplacingKey = false
                                        apiKey = ""; apiSecret = ""
                                        statusMessage = null
                                    }
                                },
                                onFailure = { statusMessage = "Not saved — biometric confirmation failed" },
                            )
                        }) { Text("Save exchange key") }
                        if (isReplacingKey) {
                            TextButton(onClick = { isReplacingKey = false; apiKey = ""; apiSecret = "" }) { Text("Cancel") }
                        }
                    }
                }
            }
        }

        item { Divider() }

        // --- 3. Coins ---
        item {
            Text("Which coins can the AI trade?", color = NeuravexColors.Silver)
        }
        items(coinOptions) { coin ->
            Row(
                modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Checkbox(
                    checked = coin in selectedCoins,
                    onCheckedChange = { checked ->
                        val updated = if (checked) selectedCoins + coin else selectedCoins - coin
                        selectedCoins = updated
                        persist(AccountSettingsUpdate(symbols = updated.toList())) { _, _ -> }
                    },
                )
                Text(coin, color = NeuravexColors.Silver)
            }
        }

        // --- 3b. Custom token (wallet venue only — a CEX pair needs no
        // on-chain contract address, only a symbol Binance already
        // recognizes) ---
        if (venue == "wallet") {
            item {
                Column {
                    Text("Add your own coin (16th slot)", color = NeuravexColors.Silver)
                    Text(
                        "Unlike the coins above, this address is not verified by NEURAVEX — " +
                            "double-check it yourself before trusting it with real funds. The AI " +
                            "also needs the symbol to be listed on Binance to analyze it at all; " +
                            "otherwise it will simply be skipped every tick, with no error.",
                        color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall,
                    )
                    OutlinedTextField(
                        value = customSymbolText, onValueChange = { customSymbolText = it.uppercase() },
                        label = { Text("Symbol (e.g. PEPE)") }, modifier = Modifier.fillMaxWidth(), singleLine = true,
                    )
                    OutlinedTextField(
                        value = customAddressText, onValueChange = { customAddressText = it.trim() },
                        label = { Text("Polygon contract address (0x...)") }, modifier = Modifier.fillMaxWidth(), singleLine = true,
                    )
                    OutlinedTextField(
                        value = customDecimalsText, onValueChange = { customDecimalsText = it.filter { c -> c.isDigit() } },
                        label = { Text("Decimals (usually 18)") },
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = KeyboardType.Number),
                        modifier = Modifier.fillMaxWidth(), singleLine = true,
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                        TextButton(onClick = {
                            val symbol = customSymbolText.trim()
                            val address = customAddressText.trim()
                            val decimals = customDecimalsText.toIntOrNull() ?: 18
                            if (symbol.isBlank() || !address.matches(Regex("0x[0-9a-fA-F]{40}"))) {
                                statusMessage = "Enter a symbol and a valid 0x… contract address (40 hex characters)"
                                return@TextButton
                            }
                            configStore.setCustomToken(symbol, address, decimals)
                            com.neuravex.app.data.PolygonTokenRegistry.setCustomToken(
                                com.neuravex.app.data.PolygonTokenRegistry.Token(symbol, address, decimals)
                            )
                            statusMessage = "Custom coin added — it now appears in the list above"
                        }) { Text("Save custom coin") }
                        if (configStore.customTokenSymbol != null) {
                            TextButton(onClick = {
                                val removedSymbol = configStore.customTokenSymbol
                                configStore.clearCustomToken()
                                com.neuravex.app.data.PolygonTokenRegistry.setCustomToken(null)
                                customSymbolText = ""; customAddressText = ""; customDecimalsText = "18"
                                // Also drop it from the current selection —
                                // a symbol the registry no longer knows
                                // about would otherwise sit in `symbols`
                                // uselessly (the agent looks it up via the
                                // same registry for execution).
                                if (removedSymbol != null) {
                                    val updated = selectedCoins.filterNot { it.startsWith("$removedSymbol/") }.toSet()
                                    if (updated != selectedCoins) {
                                        selectedCoins = updated
                                        persist(AccountSettingsUpdate(symbols = updated.toList())) { _, _ -> }
                                    }
                                }
                            }) { Text("Remove custom coin") }
                        }
                    }
                }
            }
        }

        item { Divider() }

        // --- 3c. Live vs. simulation ---
        item {
            Column {
                Text("Live trading or simulation?", color = NeuravexColors.Silver)
                Text(
                    "Simulation trades against a fictional balance you set below — nothing real " +
                        "is ever bought or sold, so you can see how the AI performs before risking " +
                        "actual money.",
                    color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    VenueButton("Simulation", selected = paperTradingEnabled) {
                        if (!paperTradingEnabled) {
                            paperTradingEnabled = true
                            persist(AccountSettingsUpdate(paper_trading_enabled = true)) { success, error ->
                                statusMessage = if (success) "Switched to simulation" else error
                            }
                        }
                    }
                    VenueButton("Live (real money)", selected = !paperTradingEnabled) {
                        if (paperTradingEnabled) {
                            val activity = context as? FragmentActivity ?: return@VenueButton
                            BiometricGate(activity).requireConfirmation(
                                title = "Switch to live trading",
                                subtitle = "NEURAVEX will start using your real ${if (venue == "wallet") "wallet" else "exchange account"} funds",
                                onSuccess = {
                                    paperTradingEnabled = false
                                    persist(AccountSettingsUpdate(paper_trading_enabled = false)) { success, error ->
                                        if (!success) { paperTradingEnabled = true; statusMessage = error }
                                        else statusMessage = "Switched to live trading"
                                    }
                                },
                                onFailure = { statusMessage = "Not switched — biometric confirmation failed" },
                            )
                        }
                    }
                }
                if (paperTradingEnabled) {
                    Text(
                        "Simulated balance: €%.2f".format(paperEquity),
                        color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall,
                    )
                    OutlinedTextField(
                        value = paperStartingBalanceText,
                        onValueChange = { paperStartingBalanceText = it.filter { c -> c.isDigit() } },
                        label = { Text("Fictional starting balance (€)") },
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = KeyboardType.Number),
                        modifier = Modifier.fillMaxWidth(), singleLine = true,
                    )
                    TextButton(onClick = {
                        val amountEur = paperStartingBalanceText.toDoubleOrNull()
                        if (amountEur == null || amountEur <= 0) {
                            statusMessage = "Enter a valid amount"
                            return@TextButton
                        }
                        // The field is labeled "(€)" and the user typed a
                        // euro amount — the backend's paper_starting_balance
                        // is USD-equivalent (see the field comment above),
                        // so convert before sending. Without this, a typed
                        // "1000" (meant as €1000) was stored as $1000 —
                        // correct on the backend, but a different number
                        // the instant the Dashboard converted it back to
                        // euros for display.
                        val rate = usdToEurRate
                        val amountUsd = if (rate != null && rate > 0) amountEur / rate else amountEur
                        persist(AccountSettingsUpdate(paper_starting_balance = amountUsd)) { success, error ->
                            statusMessage = if (success) "Simulation reset to €%.0f".format(amountEur) else error
                            if (success) paperEquity = amountEur
                        }
                    }) { Text("Reset simulation to this amount") }
                }
            }
        }

        item { Divider() }

        // --- 4. The one stop amount + the big switch ---
        item {
            Column {
                Text("Stop trading when my balance reaches:", color = NeuravexColors.Silver)
                OutlinedTextField(
                    value = balanceFloorText,
                    onValueChange = { balanceFloorText = it.filter { c -> c.isDigit() } },
                    label = { Text("Amount (€)") },
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true,
                )
                TextButton(onClick = {
                    val amount = balanceFloorText.toDoubleOrNull()
                    if (amount == null || amount <= 0) {
                        statusMessage = "Enter a valid amount"
                        return@TextButton
                    }
                    persist(AccountSettingsUpdate(min_balance_floor = amount)) { success, error ->
                        statusMessage = if (success) "Saved" else error
                    }
                }) { Text("Save stop amount") }
            }
        }

        item { Divider() }

        // Per-trade take-profit — redesigned from an account-wide
        // target: "als de winst op die trade €50 is dan stopt hij, en
        // dat voor alle trades hetzelfde." Loss stays account-wide on
        // purpose (see "Stop trading when my balance reaches" above,
        // unchanged) — only the profit side became per-position.
        item {
            Column {
                Text("Sell a trade once its own profit reaches:", color = NeuravexColors.Silver)
                Text(
                    "Applies to every open position individually — e.g. €50 means each " +
                        "trade sells itself as soon as it, on its own, has made €50. Other " +
                        "open trades keep running. Optional — leave blank to hold positions " +
                        "regardless of profit.",
                    color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall,
                )
                OutlinedTextField(
                    value = profitTargetText,
                    onValueChange = { profitTargetText = it.filter { c -> c.isDigit() } },
                    label = { Text("Amount (€) per trade") },
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth(),
                    singleLine = true,
                )
                TextButton(onClick = {
                    val amount = profitTargetText.toDoubleOrNull()
                    if (amount == null || amount <= 0) {
                        statusMessage = "Enter a valid amount"
                        return@TextButton
                    }
                    persist(AccountSettingsUpdate(max_balance_target = amount)) { success, error ->
                        statusMessage = if (success) "Saved" else error
                    }
                }) { Text("Save profit target") }
            }
        }

        item {
            statusMessage?.let { Text(it, color = if (it == "Saved" || it == "Key removed from this device") NeuravexColors.Profit else NeuravexColors.Loss) }
        }

        item { Divider() }

        item {
            Button(
                onClick = {
                    if (aiEnabled) {
                        // Stopping is safety-critical, so — unlike
                        // starting — a failed save here must NEVER be
                        // silently ignored. Previously this flipped the
                        // button to "Start AI trading" immediately and
                        // discarded the result of the save; if that save
                        // failed (a network blip, the server briefly
                        // unreachable), the backend kept ai_enabled=true
                        // and kept trading with no visible sign anything
                        // was wrong — exactly the "I pressed stop and it
                        // didn't stop" symptom this fixes.
                        persist(AccountSettingsUpdate(ai_enabled = false)) { success, error ->
                            if (success) {
                                aiEnabled = false
                                statusMessage = "AI trading stopped"
                            } else {
                                statusMessage = "STOP DID NOT REACH THE SERVER — AI trading may still be active: $error"
                            }
                        }
                        return@Button
                    }
                    if (!paperTradingEnabled && !hasCredential) {
                        statusMessage = "Add your ${if (venue == "wallet") "wallet" else "exchange"} key first"
                        return@Button
                    }
                    if (selectedCoins.isEmpty()) {
                        statusMessage = "Select at least one coin first"
                        return@Button
                    }
                    if (balanceFloorText.toDoubleOrNull() == null) {
                        statusMessage = "Set your stop amount first"
                        return@Button
                    }
                    val activity = context as? FragmentActivity ?: return@Button
                    BiometricGate(activity).requireConfirmation(
                        title = "Activate AI trading",
                        subtitle = "NEURAVEX will start trading automatically within the limits you've set",
                        onSuccess = {
                            aiEnabled = true
                            persist(AccountSettingsUpdate(ai_enabled = true)) { success, error ->
                                if (!success) {
                                    aiEnabled = false
                                    statusMessage = error
                                }
                            }
                        },
                        onFailure = { statusMessage = "Not activated — biometric confirmation failed" },
                    )
                },
                modifier = Modifier.fillMaxWidth(),
                colors = if (aiEnabled) {
                    ButtonDefaults.buttonColors(containerColor = NeuravexColors.Loss)
                } else {
                    ButtonDefaults.buttonColors(containerColor = NeuravexColors.Profit)
                },
            ) {
                Text(if (aiEnabled) "Stop AI trading" else "Start AI trading", color = NeuravexColors.Void)
            }
        }
    }
}

@Composable
private fun VenueButton(label: String, selected: Boolean, onClick: () -> Unit) {
    Button(
        onClick = onClick,
        colors = if (selected) {
            ButtonDefaults.buttonColors(containerColor = NeuravexColors.Violet)
        } else {
            ButtonDefaults.buttonColors(containerColor = NeuravexColors.SurfaceRaised)
        },
    ) {
        Text(label, color = NeuravexColors.Silver)
    }
}
