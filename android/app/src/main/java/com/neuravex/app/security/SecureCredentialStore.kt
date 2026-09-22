package com.neuravex.app.security

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.google.crypto.tink.Aead
import com.google.crypto.tink.aead.AeadConfig
import com.google.crypto.tink.integration.android.AndroidKeysetManager
import kotlinx.coroutines.flow.first
import java.util.Base64

private val Context.secureDataStore: DataStore<Preferences> by preferencesDataStore(
    name = "neuravex_secure_credentials",
)

/**
 * Encrypted local storage for trading credentials (exchange API key/secret,
 * wallet private key) and the backend session JWT. Uses Tink (Google's
 * crypto library) with an Android Keystore-backed master key — this
 * replaces the older EncryptedSharedPreferences approach, which Google
 * officially deprecated in April 2025.
 *
 * Nothing stored here is ever sent to the NEURAVEX backend — trading
 * credentials are used locally, on-device, to sign requests/transactions
 * directly against the exchange or the blockchain.
 */
class SecureCredentialStore(private val context: Context) {

    init {
        AeadConfig.register()
    }

    private fun getAead(): Aead {
        val keysetManager = AndroidKeysetManager.Builder()
            .withSharedPref(context, KEYSET_NAME, KEYSET_PREF_FILE_NAME)
            .withKeyTemplate(com.google.crypto.tink.aead.AeadKeyTemplates.AES256_GCM)
            .withMasterKeyUri("$ANDROID_KEYSTORE_URI_PREFIX$MASTER_KEY_ALIAS")
            .build()
        return keysetManager.keysetHandle.getPrimitive(Aead::class.java)
    }

    private fun encrypt(plaintext: String): String {
        val ciphertext = getAead().encrypt(plaintext.toByteArray(Charsets.UTF_8), null)
        return Base64.getEncoder().encodeToString(ciphertext)
    }

    private fun decrypt(encoded: String): String {
        val ciphertext = Base64.getDecoder().decode(encoded)
        return getAead().decrypt(ciphertext, null).toString(Charsets.UTF_8)
    }

    // --- Exchange API key/secret (CEX flow) -------------------------------

    suspend fun saveExchangeCredentials(apiKey: String, apiSecret: String) {
        context.secureDataStore.edit { prefs ->
            prefs[KEY_API_KEY] = encrypt(apiKey)
            prefs[KEY_API_SECRET] = encrypt(apiSecret)
        }
    }

    suspend fun getApiKey(): String? {
        val stored = context.secureDataStore.data.first()[KEY_API_KEY] ?: return null
        return decrypt(stored)
    }

    suspend fun getApiSecret(): String? {
        val stored = context.secureDataStore.data.first()[KEY_API_SECRET] ?: return null
        return decrypt(stored)
    }

    suspend fun hasCredentials(): Boolean = getApiKey() != null && getApiSecret() != null

    suspend fun clearExchangeCredentials() {
        context.secureDataStore.edit { prefs ->
            prefs.remove(KEY_API_KEY)
            prefs.remove(KEY_API_SECRET)
        }
    }

    // --- Backend session JWT ----------------------------------------------

    suspend fun saveBackendToken(token: String) {
        context.secureDataStore.edit { prefs -> prefs[KEY_BACKEND_TOKEN] = encrypt(token) }
    }

    suspend fun getBackendToken(): String? {
        val stored = context.secureDataStore.data.first()[KEY_BACKEND_TOKEN] ?: return null
        return decrypt(stored)
    }

    /** Synchronous variant for the one call site that cannot be a suspend
     * function: OkHttp's Interceptor.intercept() (see ApiClientFactory.kt).
     * Safe here because OkHttp interceptors already run on a background
     * dispatcher thread, never the main thread. */
    fun getBackendTokenBlocking(): String? = kotlinx.coroutines.runBlocking {
        getBackendToken()
    }

    // --- Wallet private key (on-chain / MetaMask flow) --------------------
    // Stored through the exact same Tink+DataStore+Keystore pipeline as the
    // exchange API key above. Kept as a clearly separate key/method pair
    // (not reusing KEY_API_SECRET) so the two credential types can never be
    // confused with each other in code, and so clearing one never
    // accidentally clears the other.

    suspend fun saveWalletPrivateKey(privateKeyHex: String) {
        context.secureDataStore.edit { prefs -> prefs[KEY_WALLET_PRIVATE_KEY] = encrypt(privateKeyHex) }
    }

    suspend fun getWalletPrivateKey(): String? {
        val stored = context.secureDataStore.data.first()[KEY_WALLET_PRIVATE_KEY] ?: return null
        return decrypt(stored)
    }

    suspend fun hasWalletKey(): Boolean = getWalletPrivateKey() != null

    /**
     * Derives and returns only the PUBLIC wallet address for the currently
     * stored private key, or null if none is stored. This is safe to show
     * in the UI (a wallet address is meant to be public — it's exactly
     * what you'd share to receive funds) and needs no network call: it's
     * pure local key derivation via web3j's Credentials class. Used by
     * Settings so the user can visually confirm the saved key controls
     * the wallet they think it does (e.g. by comparing it against the
     * address shown for "Account 1" in MetaMask) — this is the
     * lowest-risk way to debug "why is my reported balance wrong"
     * without ever touching or displaying the key itself.
     */
    suspend fun getWalletAddress(): String? {
        val privateKey = getWalletPrivateKey() ?: return null
        return try {
            org.web3j.crypto.Credentials.create(privateKey.trim()).address
        } catch (e: Exception) {
            null
        }
    }

    /** Removes the stored wallet private key from this device. Does NOT
     * touch the wallet itself (nothing on-chain changes) — this only
     * forgets the key locally, so the app can no longer sign transactions
     * for it until a key (the same one, or a different one) is entered
     * again. Used by the "Remove key" / "Change key" action in Settings. */
    suspend fun clearWalletKey() {
        context.secureDataStore.edit { prefs -> prefs.remove(KEY_WALLET_PRIVATE_KEY) }
    }

    companion object {
        private const val KEYSET_PREF_FILE_NAME = "neuravex_tink_keyset_prefs"
        private const val KEYSET_NAME = "neuravex_master_keyset"
        private const val MASTER_KEY_ALIAS = "neuravex_master_key"
        private const val ANDROID_KEYSTORE_URI_PREFIX = "android-keystore://"

        private val KEY_API_KEY = stringPreferencesKey("exchange_api_key")
        private val KEY_API_SECRET = stringPreferencesKey("exchange_api_secret")
        private val KEY_BACKEND_TOKEN = stringPreferencesKey("backend_jwt")
        private val KEY_WALLET_PRIVATE_KEY = stringPreferencesKey("wallet_private_key")
    }
}
