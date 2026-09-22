package com.neuravex.app.data

import com.neuravex.app.security.SecureCredentialStore
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
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
            .build()

        return Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(NeuravexApi::class.java)
    }
}
