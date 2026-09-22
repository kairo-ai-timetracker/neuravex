package com.neuravex.app.data

import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.Field
import retrofit2.http.FormUrlEncoded
import retrofit2.http.GET
import retrofit2.http.PATCH
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

interface NeuravexApi {

    @FormUrlEncoded
    @POST("/api/auth/login")
    suspend fun login(
        @Field("username") username: String,
        @Field("password") password: String,
    ): Response<TokenResponse>

    @GET("/api/dashboard/{accountId}/overview")
    suspend fun getOverview(@Path("accountId") accountId: String): Response<AccountOverview>

    @GET("/api/dashboard/{accountId}/positions")
    suspend fun getPositions(@Path("accountId") accountId: String): Response<List<PositionOut>>

    @GET("/api/dashboard/{accountId}/decisions")
    suspend fun getDecisions(
        @Path("accountId") accountId: String,
        @Query("limit") limit: Int = 50,
        // The phone always requests the last 24h (spec: "op mijn
        // smartphone liet die dat enkel bijhouden tot 24 uur") — the
        // server itself keeps every decision forever regardless of what
        // any client asks for; this only limits what this one screen
        // displays.
        @Query("since_hours") sinceHours: Int? = 24,
    ): Response<List<DecisionOut>>

    @GET("/api/dashboard/{accountId}/daily-stats")
    suspend fun getDailyStats(
        @Path("accountId") accountId: String,
        @Query("days") days: Int = 14,
    ): Response<List<DailyDecisionStats>>

    @GET("/api/settings/{accountId}")
    suspend fun getSettings(@Path("accountId") accountId: String): Response<AccountSettingsOut>

    @PATCH("/api/settings/{accountId}")
    suspend fun updateSettings(
        @Path("accountId") accountId: String,
        @Body update: AccountSettingsUpdate,
    ): Response<AccountSettingsOut>

    @GET("/api/execution/{accountId}/pending")
    suspend fun getPendingExecutions(@Path("accountId") accountId: String): Response<List<PendingExecutionOut>>

    @POST("/api/execution/{executionId}/claim")
    suspend fun claimExecution(@Path("executionId") executionId: String): Response<Unit>

    @POST("/api/execution/{executionId}/report")
    suspend fun reportExecution(
        @Path("executionId") executionId: String,
        @Body report: ReportExecutionRequest,
    ): Response<Unit>

    @POST("/api/emergency/{accountId}/kill-switch")
    suspend fun activateKillSwitch(@Path("accountId") accountId: String): Response<Unit>

    @POST("/api/dashboard/{accountId}/report-balance")
    suspend fun reportBalance(
        @Path("accountId") accountId: String,
        @Body report: ReportBalanceRequest,
    ): Response<Unit>
}
