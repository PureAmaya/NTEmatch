package render

import (
	"image"
	"image/color"
	"math"
	"strconv"
	"strings"
	"unicode"
)

// 不落在行首的标点：折行时它们跟着前一个字走（「。」独占一行很难看）。
const trailingPunct = "。，、；：？！）】》」』”’…%,.;:?!) ]}"

// tokens 按「中文逐字、西文成串」切词——中文没有空格，只能逐字换行。
func tokens(text string) []string {
	var out []string
	var buf strings.Builder
	flush := func() {
		if buf.Len() > 0 {
			out = append(out, buf.String())
			buf.Reset()
		}
	}
	for _, r := range text {
		if r < 128 && (unicode.IsLetter(r) || unicode.IsDigit(r) || strings.ContainsRune("-_/+.", r)) {
			buf.WriteRune(r)
			continue
		}
		flush()
		out = append(out, string(r))
	}
	flush()

	merged := make([]string, 0, len(out))
	for _, tok := range out {
		if len(merged) > 0 && tok != "" && isAllPunct(tok) {
			merged[len(merged)-1] += tok
			continue
		}
		merged = append(merged, tok)
	}
	if len(merged) == 0 {
		return []string{""}
	}
	return merged
}

func isAllPunct(s string) bool {
	for _, r := range s {
		if !strings.ContainsRune(trailingPunct, r) {
			return false
		}
	}
	return true
}

// Wrap 把一段文字折成若干行（中文逐字断，西文词尽量不切）。
func Wrap(text string, f *Face, maxWidth float64) []string {
	var lines []string
	cur := ""
	for _, tok := range tokens(text) {
		if cur != "" && f.Width(cur+tok) > maxWidth {
			lines = append(lines, cur)
			cur = ""
		}
		cur += tok
		// 单个词（一长串英文 / 数字）比整行还宽：按宽度硬切，别让它溢出画布
		for {
			runes := []rune(cur)
			if len(runes) <= 1 || f.Width(cur) <= maxWidth {
				break
			}
			cut := len(runes) / 2
			for cut > 1 && f.Width(string(runes[:cut])) > maxWidth {
				cut--
			}
			lines = append(lines, string(runes[:cut]))
			cur = string(runes[cut:])
		}
	}
	if cur != "" {
		lines = append(lines, cur)
	}
	if len(lines) == 0 {
		return []string{""}
	}
	return lines
}

// WrapMultiline 与 Wrap 相同，但**保留用户敲的换行**（简介会分行写地图 / 规则 / 注意事项）。
func WrapMultiline(text string, f *Face, maxWidth float64) []string {
	var out []string
	for _, para := range strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n") {
		out = append(out, Wrap(para, f, maxWidth)...)
	}
	if len(out) == 0 {
		return []string{""}
	}
	return out
}

// Clip 把一段文字截到给定宽度（放不下就加省略号）——方框里没有滚动条。
func Clip(text string, f *Face, maxWidth float64) string {
	if f.Width(text) <= maxWidth {
		return text
	}
	runes := []rune(text)
	for len(runes) > 0 && f.Width(string(runes)+"…") > maxWidth {
		runes = runes[:len(runes)-1]
	}
	if len(runes) == 0 {
		return ""
	}
	return string(runes) + "…"
}

// SectionHeader 画区块标题：**不垫色块**——斜切的渐变竖条 + 白字 + 一条细横线。
// 返回占用的高度（调用方按它推进 y）。
func SectionHeader(dst *image.RGBA, x, y, right int, title string, f *Face, note string, noteFace *Face) int {
	barW, barH := 5, f.Size()+2
	bar := rampImage(barW+4, barH, []color.RGBA{Accent, Accent2}, true)
	for row := 0; row < barH; row++ {
		// 斜切：上端偏右、下端偏左（与网页 h2::before 的 skewX 同向）
		shift := 4 - 4*row/barH
		for col := 0; col < barW; col++ {
			blend(dst, x+shift+col, y+1+row, bar.RGBAAt(col, row))
		}
	}
	DrawText(dst, x+16, y, title, f, Text)
	if note != "" && noteFace != nil {
		noteX := x + 16 + int(f.Width(title)) + 16
		dy := (f.Size() - noteFace.Size()) / 2
		if dy < 0 {
			dy = 0
		}
		DrawText(dst, noteX, y+dy, note, noteFace, Dim)
	}
	height := f.Size() + 22
	HLine(dst, x, right, y+height-8, Line)
	return height
}

// DrawHeader 画卡片顶部：警示胶带 + 渐变标题 + 「///」副标题 + 渐变条。返回新的 y。
//
// 与网页顶栏同一套：胶带、青→白→品红渐变字、斜杠分隔。**不描边、不投影**。
func DrawHeader(dst *image.RGBA, width int, title string, bits []string) int {
	pad := PadFor(width)
	inner := width - pad*2
	subFace, _ := FaceFor(24, false)

	HazardTape(dst, 0, 0, width, 6)
	y := 56
	text := title
	if text == "" {
		text = "比赛"
	}
	titleFace := FitFace(text, inner, []int{56, 50, 44}, true)
	for _, line := range firstN(Wrap(text, titleFace, float64(inner)), 2) {
		GradientText(dst, pad, y, line, titleFace, TitleStops)
		y += int(float64(titleFace.Size()) * 1.25)
	}
	if len(bits) > 0 {
		DrawText(dst, pad, y, "///", subFace, Accent2)
		offset := int(subFace.Width("///")) + 12
		line := Clip(strings.Join(bits, " · "), subFace, float64(inner-offset))
		DrawText(dst, pad+offset, y, line, subFace, Accent)
		y += int(float64(subFace.Size()) * 1.7)
	}
	GradientBar(dst, pad, y, minInt(320, inner), 4, []color.RGBA{Accent, Accent2})
	return y + 26
}

func firstN(list []string, n int) []string {
	if len(list) <= n {
		return list
	}
	return list[:n]
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func itoa(v int) string { return strconv.Itoa(v) }

// numText 把 JSON 里的数字（可能解成 int / float64 / string）转成展示文本。
func numText(v any) string {
	switch x := v.(type) {
	case nil:
		return "0"
	case string:
		return x
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case float64:
		if x == math.Trunc(x) {
			return strconv.FormatInt(int64(x), 10)
		}
		return strconv.FormatFloat(x, 'f', -1, 64)
	}
	return "0"
}
