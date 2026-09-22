package com.neuravex.app.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import com.neuravex.app.ui.NeuravexColors

private val NeuravexDarkScheme = darkColorScheme(
    primary = NeuravexColors.Violet,
    secondary = NeuravexColors.VioletGlow,
    background = NeuravexColors.Void,
    surface = NeuravexColors.Surface,
    onBackground = NeuravexColors.Silver,
    onSurface = NeuravexColors.Silver,
)

@Composable
fun NeuravexTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = NeuravexDarkScheme, content = content)
}
