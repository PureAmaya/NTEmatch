package render

import (
	"image"
	"image/color"
	"math"
	"sort"
)

// HelpPayload 是机器人帮助图的内容（由 Python 侧的 helpcard_content 提供）。
type HelpPayload struct {
	Label        string        `json:"label"`
	Title        string        `json:"title"`
	Subtitle     string        `json:"subtitle"`
	Sections     []HelpSection `json:"sections"`
	PrivacyTitle string        `json:"privacyTitle"`
	Privacy      [][2]string   `json:"privacy"`
}

type HelpSection struct {
	Title string     `json:"title"`
	Note  string     `json:"note"`
	Rows  []HelpItem `json:"rows"`
}

type HelpItem struct {
	Cmd  string `json:"cmd"`
	Desc string `json:"desc"`
}

// helpCanvasH 是帮助图的画布上限（先画到够高，最后按用掉的高度裁）。
const helpCanvasH = 5600

// RenderHelp 画帮助图。
func RenderHelp(p HelpPayload) *image.RGBA {
	width := PickWidth(func(w int) int { return composeHelp(nil, w, p) }, 0)
	return finishLight(width, helpCanvasH, func(dst *image.RGBA) int { return composeHelp(dst, width, p) })
}

// finishLight 与 finish 相同，只是高度上限不同（帮助图的画布上限更小）。
func finishLight(width, maxH int, compose func(dst *image.RGBA) int) *image.RGBA {
	img := NewCanvas(width, maxH)
	Halftone(img, image.Rect(width-300, 0, width, 200), Accent2, 12, 2, 52, "tr")
	used := compose(img)
	if used > maxH {
		used = maxH
	}
	if used < 1 {
		used = 1
	}
	return img.SubImage(image.Rect(0, 0, width, used)).(*image.RGBA)
}

func composeHelp(dst *image.RGBA, width int, p HelpPayload) int {
	pad := PadFor(width)
	inner := width - pad*2
	right := width - pad

	fLabel, _ := FaceFor(22, true)
	fSmall, _ := FaceFor(21, false)
	fSec, _ := FaceFor(28, true)
	fCmd, _ := FaceFor(25, true)
	fDesc, _ := FaceFor(23, false)
	fSub, _ := FaceFor(25, false)

	// —— 标题区：警示胶带 + 品红小标签 + 渐变标题 + 渐变条 ——
	HazardTape(dst, 0, 0, width, 6)
	y := 64
	DrawText(dst, pad, y, p.Label, fLabel, Accent2)
	y += 40
	titleFace := FitFace(p.Title, inner, []int{60, 54, 48}, true)
	GradientText(dst, pad, y, p.Title, titleFace, TitleStops)
	y += int(float64(titleFace.Size()) * 1.28)
	GradientBar(dst, pad, y, minInt(360, inner), 5, []color.RGBA{Accent, Accent2})
	y += 24
	DrawText(dst, pad, y, p.Subtitle, fSub, Dim)
	y += 64

	const rowH = 54
	const descH = 34
	cmdX := pad + 44
	innerRight := right - 24

	for _, section := range p.Sections {
		y += SectionHeader(dst, pad, y, right, section.Title, fSec, "", nil) + 12

		type laid struct {
			cmd    string
			parts  []string
			height int
			inline bool
			descX  int
		}
		laidRows := make([]laid, 0, len(section.Rows))
		for _, item := range section.Rows {
			text := "—— " + item.Desc
			headW := cmdX + int(fCmd.Width(item.Cmd)) + 14
			if fDesc.Width(text) <= float64(innerRight-headW) {
				laidRows = append(laidRows, laid{cmd: item.Cmd, parts: []string{text}, height: rowH, inline: true, descX: headW})
			} else {
				parts := Wrap(text, fDesc, float64(innerRight-(cmdX+22)))
				laidRows = append(laidRows, laid{
					cmd: item.Cmd, parts: parts, height: 34 + descH*len(parts) + 10, descX: headW,
				})
			}
		}
		noteLines := Wrap(section.Note, fSmall, float64(innerRight-(cmdX+22)))
		if section.Note == "" {
			noteLines = nil
		}
		bodyH := 20 + descH*len(noteLines) + 12
		for _, r := range laidRows {
			bodyH += r.height
		}
		Panel(dst, image.Rect(pad, y, right, y+bodyH), 20, 10, PanelFill, Line)

		ry := y + 20
		for _, r := range laidRows {
			fillCircle(dst, float64(pad+27), float64(ry+15), 3.5, Accent)
			DrawText(dst, cmdX, ry, r.cmd, fCmd, Accent)
			if r.inline {
				DrawText(dst, r.descX, ry+2, r.parts[0], fDesc, Dim)
			} else {
				ty := ry + 34
				for _, part := range r.parts {
					DrawText(dst, cmdX+22, ty, part, fDesc, Dim)
					ty += descH
				}
			}
			ry += r.height
		}
		for i, line := range noteLines {
			DrawText(dst, cmdX+22, ry+6+i*descH, line, fSmall, Accent2)
		}
		y += bodyH + 24
	}

	y += privacyBlock(dst, pad, right, width, y, p, fCmd, fDesc) + 24

	// —— 收尾：一条细线 + 底部胶带（与顶栏首尾呼应）——
	y += 6
	HLine(dst, pad, right, y, Line)
	y += 18
	HazardTape(dst, 0, y, width, y+5)
	if y+44 > helpCanvasH {
		return helpCanvasH
	}
	return y + 44
}

// privacyBlock 画「哪些在群里回、哪些只私聊发你」那一块。
func privacyBlock(dst *image.RGBA, pad, right, width, y int, p HelpPayload, fCmd, fDesc *Face) int {
	if len(p.Privacy) == 0 {
		return 0
	}
	labelW := 0
	for _, pair := range p.Privacy {
		if w := int(fCmd.Width(pair[0])) + 20; w > labelW {
			labelW = w
		}
	}
	textW := float64(width - pad*2 - 44 - labelW)
	type row struct {
		label string
		lines []string
	}
	rows := make([]row, 0, len(p.Privacy))
	for _, pair := range p.Privacy {
		rows = append(rows, row{label: pair[0], lines: Wrap(pair[1], fDesc, textW)})
	}
	bodyH := 18 + 42 + 14
	for _, r := range rows {
		bodyH += len(r.lines) * 36
	}
	Panel(dst, image.Rect(pad, y, right, y+bodyH), 20, 10, PanelFill, Line)
	ry := y + 16
	DrawText(dst, pad+22, ry, p.PrivacyTitle, fCmd, Accent)
	ry += 42
	for _, r := range rows {
		DrawText(dst, pad+22, ry, r.label, fCmd, Accent2)
		for _, line := range r.lines {
			DrawText(dst, pad+22+labelW, ry, line, fDesc, Dim)
			ry += 36
		}
	}
	return bodyH
}

// --------------------------------------------------------------------------- //
// 分享图（og:image）
// --------------------------------------------------------------------------- //
// SharePayload 是分享图的内容（由 tools/make_share_card.py 传进来）。
type SharePayload struct {
	Title   string `json:"title"`
	Tagline string `json:"tagline"`
	Note    string `json:"note"`
}

const (
	shareW = 1200
	shareH = 630
)

// RenderShare 画 1200×630 的分享图。
func RenderShare(p SharePayload) *image.RGBA {
	dst := NewCanvas(shareW, shareH)
	Halftone(dst, image.Rect(shareW-340, 0, shareW, 220), Accent2, 12, 2, 62, "tr")
	Halftone(dst, image.Rect(0, shareH-190, 260, shareH), Accent, 12, 2, 40, "bl")

	// 右下角的品红斜切
	fillPolygon(dst, [][2]int{{shareW, shareH - 150}, {shareW, shareH}, {shareW - 190, shareH}}, color.RGBA{Accent2.R, Accent2.G, Accent2.B, 210})
	fillPolygon(dst, [][2]int{{shareW, shareH - 178}, {shareW, shareH - 150}, {shareW - 28, shareH - 150}}, color.RGBA{Accent2.R, Accent2.G, Accent2.B, 90})

	gradRing(dst, 168, 296, 78, 7, 255)
	gradRing(dst, 168, 296, 41, 5, 200)
	fillCircle(dst, 168, 296, 11, Accent)

	// 四角括号
	bracket := color.RGBA{Accent.R, Accent.G, Accent.B, 150}
	for _, corner := range [][4]int{
		{34, 34, 1, 1}, {shareW - 34, 34, -1, 1}, {34, shareH - 34, 1, -1}, {shareW - 34, shareH - 34, -1, -1},
	} {
		x, y, dx, dy := corner[0], corner[1], corner[2], corner[3]
		ThickLine(dst, x, y, x+46*dx, y, 3, bracket)
		ThickLine(dst, x, y, x, y+46*dy, 3, bracket)
	}

	x, y := 316, 196
	titleFace := FitFace(p.Title, shareW-x-60, []int{84, 74, 64}, true)
	GradientText(dst, x, y, p.Title, titleFace, TitleStops)
	GradientBar(dst, x, y+112, 330, 4, []color.RGBA{Accent, Accent2})

	tagFace, _ := FaceFor(27, false)
	cx := x + 2
	for _, r := range p.Tagline {
		DrawText(dst, cx, y+142, string(r), tagFace, Dim)
		cx += int(tagFace.Width(string(r))) + 5
	}
	noteFace, _ := FaceFor(22, false)
	DrawText(dst, x, 402, p.Note, noteFace, Mute)

	HazardTape(dst, 0, 0, shareW, 7)
	HazardTape(dst, 0, shareH-7, shareW, shareH)
	return dst
}

// gradRing 用一串短线段拼出「青 ↔ 品红」的渐变环（三角波：两端青、中间品红）。
func gradRing(dst *image.RGBA, cx, cy, r, thick, alpha int) {
	const segs = 180
	for i := 0; i < segs; i++ {
		t0 := float64(i) / segs * 2 * math.Pi
		t1 := float64(i+1) / segs * 2 * math.Pi
		k := 1 - math.Abs(2*float64(i)/segs-1)
		c := pickStop([]color.RGBA{Accent, Accent2}, k)
		c.A = uint8(alpha)
		x0 := float64(cx) + float64(r)*math.Cos(t0)
		y0 := float64(cy) + float64(r)*math.Sin(t0)
		x1 := float64(cx) + float64(r)*math.Cos(t1)
		y1 := float64(cy) + float64(r)*math.Sin(t1)
		ThickLine(dst, int(x0), int(y0), int(x1), int(y1), thick, c)
	}
}

// fillPolygon 逐行扫描填充任意简单多边形（只用于右下角那个三角形）。
func fillPolygon(dst *image.RGBA, pts [][2]int, c color.RGBA) {
	minY, maxY := pts[0][1], pts[0][1]
	for _, p := range pts {
		if p[1] < minY {
			minY = p[1]
		}
		if p[1] > maxY {
			maxY = p[1]
		}
	}
	for y := minY; y < maxY; y++ {
		yc := float64(y) + 0.5
		crossings := []float64{}
		for i := 0; i < len(pts); i++ {
			a, b := pts[i], pts[(i+1)%len(pts)]
			ay, by := float64(a[1]), float64(b[1])
			if (ay <= yc && by > yc) || (by <= yc && ay > yc) {
				t := (yc - ay) / (by - ay)
				crossings = append(crossings, float64(a[0])+t*float64(b[0]-a[0]))
			}
		}
		if len(crossings) < 2 {
			continue
		}
		// 交点必须**按 x 排序**才能两两配对：按边的顺序拿到的两个交点可能是
		// 「右、左」，那样 fillSpan 拿到 x0 > x1 会直接什么都不画（右下角那块
		// 斜切就是这么整块消失的——图上少了它，很难一眼看出是这里的问题）。
		sort.Float64s(crossings)
		for i := 0; i+1 < len(crossings); i += 2 {
			fillSpan(dst, y, crossings[i], crossings[i+1], c)
		}
	}
}
