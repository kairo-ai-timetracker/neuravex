package com.neuravex.app.security

import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity

class BiometricGate(private val activity: FragmentActivity) {

    fun requireConfirmation(
        title: String,
        subtitle: String,
        onSuccess: () -> Unit,
        onFailure: (String) -> Unit,
    ) {
        try {
            val allowedAuthenticators = BiometricManager.Authenticators.BIOMETRIC_STRONG or
                BiometricManager.Authenticators.DEVICE_CREDENTIAL

            // Check BEFORE showing anything — this is what surfaces the
            // actual reason nothing happens on devices with no fingerprint
            // enrolled, no secure lock screen set at all, or no biometric
            // hardware, instead of the button silently doing nothing.
            val canAuthenticate = BiometricManager.from(activity).canAuthenticate(allowedAuthenticators)
            if (canAuthenticate != BiometricManager.BIOMETRIC_SUCCESS) {
                onFailure(describeUnavailableReason(canAuthenticate))
                return
            }

            val executor = ContextCompat.getMainExecutor(activity)
            val callback = object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    onSuccess()
                }

                override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                    onFailure("$errString (error code $errorCode)")
                }

                override fun onAuthenticationFailed() {
                    onFailure("Authentication failed — fingerprint/face/PIN did not match")
                }
            }

            val prompt = BiometricPrompt(activity, executor, callback)
            val promptInfo = BiometricPrompt.PromptInfo.Builder()
                .setTitle(title)
                .setSubtitle(subtitle)
                .setAllowedAuthenticators(allowedAuthenticators)
                .build()

            prompt.authenticate(promptInfo)
        } catch (e: Exception) {
            // Never let this fail silently — a caught-and-swallowed
            // exception here is exactly what makes a "Save" button look
            // like it does nothing at all.
            onFailure("Biometric prompt error: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun describeUnavailableReason(code: Int): String = when (code) {
        BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE ->
            "This device has no biometric hardware and no secure lock screen (PIN/pattern/password) set"
        BiometricManager.BIOMETRIC_ERROR_HW_UNAVAILABLE ->
            "Biometric hardware is currently unavailable"
        BiometricManager.BIOMETRIC_ERROR_NONE_ENROLLED ->
            "No fingerprint/face is enrolled, and no PIN/pattern/password is set as a screen lock — " +
                "set one in your phone's Settings > Security first"
        BiometricManager.BIOMETRIC_ERROR_SECURITY_UPDATE_REQUIRED ->
            "A security update is required before biometric authentication can be used"
        BiometricManager.BIOMETRIC_ERROR_UNSUPPORTED ->
            "Biometric authentication is not supported on this device/Android version"
        else -> "Biometric authentication unavailable (code $code)"
    }
}
