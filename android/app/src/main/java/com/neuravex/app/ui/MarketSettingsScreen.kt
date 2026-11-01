package com.neuravex.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
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
import androidx.compose.ui.unit.dp
import com.neuravex.app.data.AccountSettingsUpdate
import com.neuravex.app.data.ApiClientFactory
import com.neuravex.app.data.AppConfigStore
import com.neuravex.app.data.CurrencyConverter
import com.neuravex.app.data.PortfolioOut
import com.neuravex.app.security.SecureCredentialStore
import kotlinx.coroutines.launch

/** Settings of a simulation-only portfolio: no wallet, keys or live switch. */
@Composable
fun MarketSettingsScreen(configStore: AppConfigStore, portfolio: PortfolioOut, onBack: () -> Unit) {
    val context = LocalContext.current
    val api = remember { ApiClientFactory.create(configStore.backendBaseUrl!!, SecureCredentialStore(context)) }
    val scope = rememberCoroutineScope()
    var selected by remember { mutableStateOf(setOf<String>()) }
    var stopText by remember { mutableStateOf("") }
    var startText by remember { mutableStateOf("") }
    var rate by remember { mutableStateOf(1.0) }
    var status by remember { mutableStateOf<String?>(null) }
    var pendingReset by remember { mutableStateOf<Double?>(null) }

    fun save(update: AccountSettingsUpdate, done: String) {
        scope.launch {
            status = try {
                val response = api.updateSettings(portfolio.account_id, update)
                if (response.isSuccessful) done else "Could not save (HTTP ${response.code()})"
            } catch (e: Exception) {
                "Could not save — check your connection"
            }
        }
    }

    LaunchedEffect(Unit) {
        try {
            rate = CurrencyConverter.getUsdToEurRate()
            api.getSettings(portfolio.account_id).body()?.let { s ->
                selected = s.symbols.toSet()
                stopText = s.min_balance_floor?.let { "%.0f".format(it * rate) } ?: ""
                startText = "%.0f".format(s.paper_starting_balance * rate)
            }
        } catch (e: Exception) {
            status = "Could not load settings — check your connection"
        }
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(20.dp),
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("${portfolio.market.replaceFirstChar { it.uppercase() }} settings", style = MaterialTheme.typography.headlineSmall, color = NeuravexColors.Silver)
                TextButton(onClick = onBack) { Text("Done") }
            }
            Text("Simulation only · no real funds", color = NeuravexColors.Aurora, style = MaterialTheme.typography.labelMedium)
        }
        item {
            CoinTiles(
                coins = portfolio.symbols,
                selected = selected,
                onChange = { updated ->
                    selected = updated
                    save(AccountSettingsUpdate(symbols = updated.toList()), "Saved")
                },
                title = "What the AI may trade",
            )
        }
        item {
            OutlinedTextField(
                value = stopText,
                onValueChange = { stopText = it.filter { c -> c.isDigit() } },
                label = { Text("Stop trading when my balance reaches (€)") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                modifier = Modifier.fillMaxWidth(), singleLine = true,
            )
            TextButton(onClick = {
                val amount = stopText.toDoubleOrNull()
                if (amount == null || amount <= 0) {
                    status = "Enter a valid amount"
                } else {
                    save(AccountSettingsUpdate(min_balance_floor = amount / rate), "Saved")
                }
            }) { Text("Save stop amount") }
        }
        item {
            OutlinedTextField(
                value = startText,
                onValueChange = { startText = it.filter { c -> c.isDigit() } },
                label = { Text("Fictional starting balance (€)") },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                modifier = Modifier.fillMaxWidth(), singleLine = true,
            )
            TextButton(onClick = {
                val amount = startText.toDoubleOrNull()
                if (amount == null || amount <= 0) status = "Enter a valid amount" else pendingReset = amount
            }) { Text("Reset simulation to this amount") }
        }
        status?.let { message -> item { Text(message, color = NeuravexColors.VioletGlow) } }
    }

    pendingReset?.let { amount ->
        AlertDialog(
            onDismissRequest = { pendingReset = null },
            title = { Text("Reset simulation?") },
            text = { Text("This deletes this portfolio's positions and history and starts over with €%.0f.".format(amount)) },
            confirmButton = {
                TextButton(onClick = {
                    pendingReset = null
                    save(AccountSettingsUpdate(paper_starting_balance = amount / rate), "Simulation reset to €%.0f".format(amount))
                }) { Text("Reset") }
            },
            dismissButton = { TextButton(onClick = { pendingReset = null }) { Text("Cancel") } },
        )
    }
}
