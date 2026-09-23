package com.neuravex.app.data

import com.neuravex.app.security.SecureCredentialStore
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import okhttp3.Authenticator
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.Route
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory

object ApiClientFactory {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    fun create(baseUrl: String, credentialStore: SecureCredentialStore): NeuravexApi {
        val client = OkHttpClient.Builder()
            .addInterceptor { chain ->
                val token = credentialStore.getBackendTokenBlocking()
                val requestBuilder = chain.request().newBuilder()
                if (token != null) {
                    requestBuilder.addHeader("Authorization", "Bearer $token")
                }
                chain.proceed(requestBuilder.build())
            }
            .authenticator(TokenRefreshAuthenticator(baseUrl, credentialStore, json))
            .build()

        return Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(NeuravexApi::class.java)
    }
}

/**
 * Called by OkHttp automatically whenever a request comes back 401 — this
 * is what makes the 12h access token invisible to the rest of the app: on
 * the first 401, this trades the stored refresh token for a new
 * access+refresh pair via POST /api/auth/refresh, saves both, and retries
 * the original request with the new access token, all before the original
 * suspend call (e.g. getSettings, getPendingExecutions in
 * TradingExecutionService's poll loop) ever sees a failure. Without this,
 * the whole app — trade execution, exit management, notifications —
 * silently stopped working every 12 hours (see core/auth.py's docstring
 * for the backend half of this).
 *
 * Uses a bare OkHttpClient with none of ApiClientFactory's own
 * interceptor/authenticator, so a failed refresh call can never
 * recursively trigger another refresh attempt against itself.
 */
private class TokenRefreshAuthenticator(
    private val baseUrl: String,
    private val credentialStore: SecureCredentialStore,
    private val json: Json,
) : Authenticator {

    private val refreshHttpClient = OkHttpClient.Builder().build()

    override fun authenticate(route: Route?, response: Response): Request? {
        // Never try to refresh the refresh call itself — a 401 there means
        // the refresh token is genuinely invalid/expired, and the user has
        // to log in again (there is no third level to fall back to).
        if (response.request.url.encodedPath.endsWith("/api/auth/refresh")) return null
        // A request that already carried a just-refreshed token and still
        // got 401 has a token that's invalid for some other reason (wrong
        // signing key, user deleted, ...) — retrying forever against a
        // server that keeps saying no would just burn battery and data.
        if (responseChainLength(response) >= 2) return null

        val refreshToken = credentialStore.getRefreshTokenBlocking() ?: return null

        return try {
            val requestBody = json.encodeToString(RefreshTokenRequest.serializer(), RefreshTokenRequest(refreshToken))
                .toRequestBody("application/json".toMediaType())
            val refreshRequest = Request.Builder()
                .url("$baseUrl/api/auth/refresh")
                .post(requestBody)
                .build()

            refreshHttpClient.newCall(refreshRequest).execute().use { refreshResponse ->
                if (!refreshResponse.isSuccessful) return null
                val bodyString = refreshResponse.body?.string() ?: return null
                val tokens = json.decodeFromString(TokenResponse.serializer(), bodyString)
                runBlocking { credentialStore.saveBackendTokens(tokens.access_token, tokens.refresh_token) }
                response.request.newBuilder()
                    .header("Authorization", "Bearer ${tokens.access_token}")
                    .build()
            }
        } catch (e: Exception) {
            // Network hiccup mid-refresh, malformed response, etc. — give
            // up on THIS request; the next poll cycle will try the whole
            // thing again with whatever token is currently stored.
            null
        }
    }

    private fun responseChainLength(response: Response): Int {
        var count = 1
        var prior = response.priorResponse
        while (prior != null) {
            count++
            prior = prior.priorResponse
        }
        return count
    }
}
