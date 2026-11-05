// Package render 是站点与机器人的出图实现（比赛卡片 / 结果卡片 / 帮助图 / 分享图）。
//
// 为什么单独一个二进制：出图是「一次画一张、画完就完」的活，不需要常驻；
// Python 侧把排版所需的**内容**（payload）交过来，这里负责画，画完写文件、回一行 JSON。
// 于是「画成什么样」只有这一份实现——配色、字号、间距、取景规则全在这包里，
// 换主题只改 theme.go，不会出现「网页一套、图一套、两套还各改各的」。
//
// 三条画法约定（与 static/css/nte.css 同一套语言）：
//
//  1. 文字不描边、不投影：物料里的字直接压在底上，唯一的「上色」手段是标题那层
//     青 → 白 → 品红渐变（gradientText）；
//  2. 装饰只用几何件：细渐变条、警示斜纹胶带、双色网点、切角面板；
//  3. 尺寸跟内容走：pickWidth 在宽度阶梯里挑一档，别让少量文字画成一张扁横幅、
//     也别让大量文字变成一条细长条。
package render

import (
	"image/color"
	"math"
)

// --------------------------------------------------------------------------- //
// 调色板（与 static/css/nte.css 的变量逐条对应，改主题只改这里）
// --------------------------------------------------------------------------- //
var (
	BG        = color.RGBA{7, 10, 18, 255}     // --bg        #070a12
	Ink       = color.RGBA{4, 6, 12, 255}      // --ink       #04060c
	PanelFill = color.RGBA{14, 20, 33, 240}    // --panel     rgb(14,20,33)
	PanelHi   = color.RGBA{19, 26, 42, 255}    // --panel-2   rgb(19,26,42)
	Accent    = color.RGBA{34, 224, 232, 255}  // --cyan      #22e0e8
	Accent2   = color.RGBA{255, 47, 142, 255}  // --magenta   #ff2f8e
	Amber     = color.RGBA{255, 216, 61, 255}  // --amber     #ffd83d
	Text      = color.RGBA{230, 240, 255, 255} // --txt      #e6f0ff
	Dim       = color.RGBA{148, 168, 194, 255} // --txt-dim  #94a8c2
	Mute      = color.RGBA{109, 128, 152, 255} // --txt-mute #6d8098
	Line      = color.RGBA{34, 224, 232, 38}   // --line     rgba(34,224,232,.15)

	// 对阵框：比面板亮一档（站点 .member / .round 的底）
	BoxFill = color.RGBA{23, 32, 50, 255}
	BoxEdge = color.RGBA{48, 63, 92, 255}
)

// TitleStops 是标题渐变（青 → 白 → 品红），对应网页的 --title-grad。
var TitleStops = []color.RGBA{Accent, {255, 255, 255, 255}, Accent2}

// --------------------------------------------------------------------------- //
// 尺寸策略
// --------------------------------------------------------------------------- //
// Widths 是画布宽度的两档：窄（内容少）/ 宽（内容多）。
// 字号是固定像素，画布越窄，字在手机屏幕上就越大——所以内容少时宁可画窄一点。
var Widths = []int{760, 1080}

// MinRatio 是「不过分细长」的下限（宽/高）。再窄就成了一条，宁可换更宽的画布。
const MinRatio = 0.42

// PadFor 是左右安全边距：跟着画布宽度微调，但不跟字号一起缩（缩了字更挤）。
func PadFor(width int) int {
	pad := int(math.Round(float64(width) * 0.0592)) // 1080 → 64
	if pad < 40 {
		pad = 40
	}
	return pad
}

// PickWidth 按内容挑画布宽度：measure(w) 给出该宽度下排版需要的高度。
//
// 只看两档，且**只量一次**（量宽的那档，它折行最少、最便宜）：在宽画布上量出高度，
// 估算「换窄画布会有多高」，窄档还能守住 MinRatio 就用窄档，否则用宽档。
// 这个估算只影响「选哪一档」，不影响裁剪——真正的裁剪高度来自随后那一趟真正的排版。
func PickWidth(measure func(int) int, minWidth int) int {
	wide := Widths[len(Widths)-1]
	if minWidth > wide {
		wide = minWidth
	}
	height := measure(wide)
	if height <= 0 {
		return wide
	}
	narrow := Widths[0]
	if minWidth > narrow {
		narrow = minWidth
	}
	if narrow >= wide {
		return wide
	}
	// 窄档的行宽只有宽档的 (narrow-2p)/(wide-2p)，折行数大致按这个比例增加
	scale := float64(wide-2*PadFor(wide)) / math.Max(1, float64(narrow-2*PadFor(narrow)))
	if float64(narrow)/(float64(height)*scale) >= MinRatio {
		return narrow
	}
	return wide
}
