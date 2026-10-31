package com.neuravex.app.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableDoubleStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin
import kotlin.random.Random

/** Slowly drifting nebula and twinkling stars behind a screen's content. */
@Composable
fun CosmicBackground(content: @Composable BoxScope.() -> Unit) {
    val time = rememberInfiniteTransition(label = "cosmos")
    val drift by time.animateFloat(
        initialValue = 0f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(40_000, easing = LinearEasing), RepeatMode.Restart),
        label = "drift",
    )
    val stars = remember {
        val random = Random(7)
        List(70) { Triple(random.nextFloat(), random.nextFloat(), random.nextFloat()) }
    }
    Box(modifier = Modifier.fillMaxSize().background(NeuravexColors.Void)) {
        Canvas(modifier = Modifier.fillMaxSize()) {
            val angle = drift * 2 * PI.toFloat()
            drawCircle(
                brush = Brush.radialGradient(
                    listOf(NeuravexColors.Violet.copy(alpha = 0.30f), Color.Transparent),
                    center = Offset(size.width * (0.5f + 0.3f * cos(angle)), size.height * 0.18f),
                    radius = size.width * 0.9f,
                ),
                radius = size.maxDimension,
            )
            drawCircle(
                brush = Brush.radialGradient(
                    listOf(NeuravexColors.Aurora.copy(alpha = 0.14f), Color.Transparent),
                    center = Offset(size.width * (0.5f - 0.35f * sin(angle)), size.height * 0.75f),
                    radius = size.width * 0.8f,
                ),
                radius = size.maxDimension,
            )
            stars.forEach { (x, y, phase) ->
                val twinkle = 0.25f + 0.55f * (0.5f + 0.5f * sin((drift * 12 + phase) * 2 * PI.toFloat()))
                drawCircle(
                    color = NeuravexColors.Silver.copy(alpha = twinkle),
                    radius = 0.8f + phase * 1.6f,
                    center = Offset(x * size.width, y * size.height),
                )
            }
        }
        content()
    }
}

/** The bot's "brain": a breathing orb with orbiting sparks. The colour is its
 * mood, and it breathes faster while it's holding a position. */
@Composable
fun NeuroOrb(mood: Color, active: Boolean, modifier: Modifier = Modifier, orbSize: Dp = 230.dp, content: @Composable BoxScope.() -> Unit) {
    val color by animateColorAsState(mood, tween(900), label = "mood")
    val time = rememberInfiniteTransition(label = "orb")
    val breath by time.animateFloat(
        initialValue = 0.90f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(if (active) 1_400 else 3_200, easing = FastOutSlowInEasing), RepeatMode.Reverse),
        label = "breath",
    )
    val spin by time.animateFloat(
        initialValue = 0f, targetValue = 2 * PI.toFloat(),
        animationSpec = infiniteRepeatable(tween(14_000, easing = LinearEasing), RepeatMode.Restart),
        label = "spin",
    )
    Box(modifier = modifier.size(orbSize), contentAlignment = Alignment.Center) {
        Canvas(modifier = Modifier.fillMaxSize()) {
            val radius = size.minDimension / 2
            drawCircle(
                brush = Brush.radialGradient(
                    listOf(color.copy(alpha = 0.55f), color.copy(alpha = 0.12f), Color.Transparent),
                    radius = radius * breath,
                ),
                radius = radius * breath,
            )
            drawCircle(color = color.copy(alpha = 0.55f), radius = radius * 0.72f, style = Stroke(1.5.dp.toPx()))
            for (i in 0 until 5) {
                val a = spin * (if (i % 2 == 0) 1f else -0.7f) + i * 1.26f
                val orbit = radius * (0.72f + 0.05f * i)
                val spark = Offset(center.x + orbit * cos(a), center.y + orbit * sin(a))
                drawCircle(color.copy(alpha = 0.35f), radius = 7.dp.toPx(), center = spark)
                drawCircle(NeuravexColors.Silver, radius = 2.dp.toPx(), center = spark)
            }
        }
        content()
    }
}

/** A frosted panel: all cards in the app share this look. */
@Composable
fun GlassCard(modifier: Modifier = Modifier, glow: Color = NeuravexColors.Violet, content: @Composable ColumnScope.() -> Unit) {
    val shape = RoundedCornerShape(20.dp)
    Column(
        modifier = modifier
            .background(NeuravexColors.SurfaceRaised.copy(alpha = 0.72f), shape)
            .border(1.dp, Brush.linearGradient(listOf(glow.copy(alpha = 0.65f), Color.White.copy(alpha = 0.05f))), shape)
            .padding(16.dp),
        content = content,
    )
}

/** Half-circle gauge for the distance to the bot's safety stop. */
@Composable
fun ArcGauge(fraction: Float, color: Color, modifier: Modifier = Modifier) {
    val shown by animateFloatAsState(fraction.coerceIn(0f, 1f), tween(900), label = "gauge")
    Canvas(modifier = modifier) {
        val stroke = 8.dp.toPx()
        val arcSize = Size(size.width - stroke, (size.width - stroke))
        val topLeft = Offset(stroke / 2, stroke / 2)
        drawArc(
            color = Color.White.copy(alpha = 0.10f), startAngle = 180f, sweepAngle = 180f, useCenter = false,
            topLeft = topLeft, size = arcSize, style = Stroke(stroke, cap = StrokeCap.Round),
        )
        drawArc(
            color = color, startAngle = 180f, sweepAngle = 180f * shown, useCenter = false,
            topLeft = topLeft, size = arcSize, style = Stroke(stroke, cap = StrokeCap.Round),
        )
    }
}

/** An amount that counts up or down to its new value instead of jumping. */
@Composable
fun CountingText(value: Double, format: (Double) -> String, color: Color, style: androidx.compose.ui.text.TextStyle = MaterialTheme.typography.headlineSmall) {
    var shown by remember { mutableDoubleStateOf(value) }
    LaunchedEffect(value) {
        val start = shown
        val progress = Animatable(0f)
        progress.animateTo(1f, tween(800, easing = FastOutSlowInEasing)) { shown = start + (value - start) * this.value }
    }
    Text(format(shown), color = color, style = style)
}

/** Coins as tiles that light up when on: all start on, tap one to switch it off. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun CoinTiles(coins: List<String>, selected: Set<String>, onChange: (Set<String>) -> Unit) {
    GlassCard(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column {
                Text("Coins the AI may trade", color = NeuravexColors.Silver, style = MaterialTheme.typography.titleMedium)
                Text("${coins.count { it in selected }} of ${coins.size} on · tap to switch", color = NeuravexColors.SilverDim, style = MaterialTheme.typography.bodySmall)
            }
            val allOn = coins.all { it in selected }
            TextButton(onClick = { onChange(if (allOn) emptySet() else coins.toSet()) }) {
                Text(if (allOn) "All off" else "All on", color = NeuravexColors.VioletGlow)
            }
        }
        FlowRow(
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            coins.forEach { coin ->
                val on = coin in selected
                val color by animateColorAsState(if (on) NeuravexColors.Profit else NeuravexColors.SilverDim, tween(250), label = "coin")
                Text(
                    coin.substringBefore("/"),
                    color = if (on) NeuravexColors.Silver else NeuravexColors.SilverDim,
                    modifier = Modifier
                        .semantics {
                            role = Role.Switch
                            stateDescription = if (on) "On" else "Off"
                        }
                        .background(color.copy(alpha = if (on) 0.18f else 0.05f), RoundedCornerShape(14.dp))
                        .border(1.dp, color.copy(alpha = if (on) 0.8f else 0.25f), RoundedCornerShape(14.dp))
                        .clickable { onChange(if (on) selected - coin else selected + coin) }
                        .padding(horizontal = 16.dp, vertical = 10.dp),
                )
            }
        }
    }
}
