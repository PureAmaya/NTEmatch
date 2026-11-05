package render

import (
	"image"
	"image/color"
	"math"
)

// MaxHeight 是画布高度上限：规则再多也不至于出一张几万像素的图。
const MaxHeight = 6000

// 对阵图的兜底尺寸：坐标与尺寸本来都由 Python 侧算好随 payload 传来（“tree.box“），
// 这里只是「payload 没给」时的退路，不是第二个定义。
const (
	treeBox     = 250
	treeBoxH    = 96
	treeSideRow = 33
	treeChampW  = 190
)

// EventPayload 是「比赛信息 + 规则」卡片的内容（由 Python 侧组装，纯数据）。
type EventPayload struct {
	Title     string      `json:"title"`
	ID        string      `json:"id"`
	Status    string      `json:"status"`
	Headline  string      `json:"headline"`
	Caption   string      `json:"caption"`
	Meta      [][2]string `json:"meta"`
	Groups    [][2]string `json:"groups"`
	Sections  []Section   `json:"sections"`
	Note      string      `json:"note"`
	NoteTitle string      `json:"noteTitle"`
}

// Section 是规则里的一节（标题 + 若干条目）。
type Section struct {
	Title string   `json:"title"`
	Items []string `json:"items"`
}

// ResultPayload 是「比赛结果」卡片的内容（小组赛表 + 逐场比分 + 淘汰赛树）。
type ResultPayload struct {
	Title       string        `json:"title"`
	ID          string        `json:"id"`
	StatusLabel string        `json:"statusLabel"`
	Headline    string        `json:"headline"`
	Caption     string        `json:"caption"`
	Champion    string        `json:"champion"`
	Groups      []ResultGroup `json:"groups"`
	Standings   []StandingRow `json:"standings"`
	Matches     []MatchLine   `json:"matches"`
	Tree        *Tree         `json:"tree"`
}

type ResultGroup struct {
	Key     string      `json:"key"`
	Note    string      `json:"note"`
	Rows    []GroupRow  `json:"rows"`
	Matches []MatchLine `json:"matches"`
}

type GroupRow struct {
	Rank      int    `json:"rank"`
	Name      string `json:"name"`
	Played    int    `json:"played"`
	Win       int    `json:"win"`
	Lose      int    `json:"lose"`
	Placement int    `json:"placement"`
	Seed      int    `json:"seed"`
}

type StandingRow struct {
	Rank    int    `json:"rank"`
	Name    string `json:"name"`
	Played  int    `json:"played"`
	Average string `json:"average"`
	Points  int    `json:"points"`
}

type MatchLine struct {
	Round string `json:"round"`
	Line  string `json:"line"`
	Done  bool   `json:"done"`
}

type Tree struct {
	Single  bool     `json:"single"`
	Box     TreeBox  `json:"box"`
	Width   int      `json:"width"`
	Height  int      `json:"height"`
	Bands   []Band   `json:"bands"`
	Columns []Column `json:"columns"`
	Nodes   []Node   `json:"nodes"`
	Links   []Link   `json:"links"`
	Champ   Champ    `json:"champ"`
}

// TreeBox 是对阵框的尺寸（由 Python 侧算坐标时一并给出）。
type TreeBox struct {
	W       int `json:"w"`
	H       int `json:"h"`
	SideRow int `json:"sideRow"`
	ChampW  int `json:"champW"`
}

type Band struct {
	X      int    `json:"x"`
	Y      int    `json:"y"`
	W      int    `json:"w"`
	H      int    `json:"h"`
	Title  string `json:"title"`
	TitleY int    `json:"titleY"`
}

type Column struct {
	X     int    `json:"x"`
	Y     int    `json:"y"`
	Title string `json:"title"`
}

type Node struct {
	X           int    `json:"x"`
	Y           int    `json:"y"`
	H           int    `json:"h"`
	Code        string `json:"code"`
	Label       string `json:"label"`
	Status      string `json:"status"`
	StatusLabel string `json:"statusLabel"`
	Sides       []Side `json:"sides"`
}

type Side struct {
	Name  string `json:"name"`
	Score string `json:"score"`
	Win   bool   `json:"win"`
	Color string `json:"color"`
}

type Link struct {
	X1   int  `json:"x1"`
	Y1   int  `json:"y1"`
	X2   int  `json:"x2"`
	Y2   int  `json:"y2"`
	Done bool `json:"done"`
}

type Champ struct {
	X    int    `json:"x"`
	Y    int    `json:"y"`
	Name string `json:"name"`
}

// --------------------------------------------------------------------------- //
// 渲染入口
// --------------------------------------------------------------------------- //
// RenderEvent 画「比赛信息 + 规则」卡片。
func RenderEvent(site string, p EventPayload) *image.RGBA {
	width := PickWidth(func(w int) int { return composeEvent(nil, w, p, site) }, 0)
	return finish(width, func(dst *image.RGBA) int { return composeEvent(dst, width, p, site) })
}

// RenderResult 画「比赛结果」卡片（宽度先由树状图定下限）。
func RenderResult(site string, p ResultPayload) *image.RGBA {
	minWidth := 0
	if p.Tree != nil {
		minWidth = p.Tree.Width + PadFor(Widths[len(Widths)-1])*2
	}
	width := PickWidth(func(w int) int { return composeResult(nil, w, p, site) }, minWidth)
	return finish(width, func(dst *image.RGBA) int { return composeResult(dst, width, p, site) })
}

// finish 造画布、画一遍、按实际用掉的高度裁掉。
func finish(width int, compose func(dst *image.RGBA) int) *image.RGBA {
	img := NewCanvas(width, MaxHeight)
	// 右上角一片品红网点（与网页卡片同一种装饰）
	Halftone(img, image.Rect(width-320, 0, width, 210), Accent2, 12, 2, 54, "tr")
	used := compose(img)
	if used > MaxHeight {
		used = MaxHeight
	}
	if used < 1 {
		used = 1
	}
	return img.SubImage(image.Rect(0, 0, width, used)).(*image.RGBA)
}

// --------------------------------------------------------------------------- //
// 比赛信息卡
// --------------------------------------------------------------------------- //
func composeEvent(dst *image.RGBA, width int, p EventPayload, site string) int {
	pad := PadFor(width)
	inner := width - pad*2
	right := pad + inner

	fKey, _ := FaceFor(24, false)
	fVal, _ := FaceFor(26, false)
	fSec, _ := FaceFor(30, true)
	fBody, _ := FaceFor(26, false)
	fNote, _ := FaceFor(22, false)
	rowH := int(float64(fVal.Size()) * 1.66)
	itemH := int(float64(fBody.Size()) * 1.42)

	y := DrawHeader(dst, width, p.Title, nonEmpty(p.ID, p.Status, p.Headline))

	kv := func(pairs [][2]string) {
		if len(pairs) == 0 {
			return
		}
		type row struct {
			key      string
			keyWidth int
			lines    []string
		}
		rows := make([]row, 0, len(pairs))
		for _, pair := range pairs {
			keyWidth := int(fKey.Width(pair[0])) + 18
			lines := Wrap(pair[1], fVal, float64(inner-keyWidth-40))
			rows = append(rows, row{key: pair[0], keyWidth: keyWidth, lines: lines})
		}
		bodyH := 16 + 10
		for _, r := range rows {
			bodyH += rowH * len(r.lines)
		}
		Panel(dst, image.Rect(pad, y, right, y+bodyH), 18, 10, PanelFill, Line)
		ry := y + 14
		for _, r := range rows {
			DrawText(dst, pad+20, ry, r.key, fKey, Dim)
			for _, line := range r.lines {
				DrawText(dst, pad+20+r.keyWidth, ry, line, fVal, Text)
				ry += rowH
			}
		}
		y += bodyH + 20
	}

	kv(p.Meta)
	if len(p.Groups) > 0 {
		y += SectionHeader(dst, pad, y, right, "分组名单", fSec, "", nil)
		kv(p.Groups)
	}

	for _, sec := range p.Sections {
		if len(sec.Items) == 0 {
			continue
		}
		y += SectionHeader(dst, pad, y, right, sec.Title, fSec, "", nil)
		for _, item := range sec.Items {
			for i, line := range Wrap(item, fBody, float64(inner-30)) {
				if i == 0 {
					DrawText(dst, pad+4, y, "·", fBody, Accent)
				}
				DrawText(dst, pad+22, y, line, fBody, Text)
				y += itemH
			}
		}
		y += 16
		if y > MaxHeight-420 {
			DrawText(dst, pad+22, y, "…（还有更多规则，见站点「比赛规则」）", fNote, Amber)
			y += 40
			break
		}
	}

	if note := trimSpace(p.Note); note != "" && y < MaxHeight-280 {
		title := p.NoteTitle
		if title == "" {
			title = "比赛简介"
		}
		y += SectionHeader(dst, pad, y, right, title, fSec, "", nil)
		for _, line := range WrapMultiline(note, fNote, float64(inner)) {
			if y > MaxHeight-150 {
				break
			}
			DrawText(dst, pad, y, line, fNote, Dim)
			y += int(float64(fNote.Size()) * 1.55)
		}
		y += 18
	}

	return footer(dst, width, y, p.Caption, site, "规则随赛制自动生成", fNote)
}

// --------------------------------------------------------------------------- //
// 比赛结果卡
// --------------------------------------------------------------------------- //
func composeResult(dst *image.RGBA, width int, p ResultPayload, site string) int {
	pad := PadFor(width)
	inner := width - pad*2
	right := pad + inner

	fKey, _ := FaceFor(22, false)
	fBody, _ := FaceFor(24, false)
	fSmall, _ := FaceFor(20, false)
	fNote, _ := FaceFor(22, false)
	fSec, _ := FaceFor(30, true)
	fBox, _ := FaceFor(21, false)
	fCode, _ := FaceFor(17, false)
	rowH := int(float64(fBody.Size()) * 1.55)
	smallH := int(float64(fSmall.Size()) * 1.6)

	y := DrawHeader(dst, width, p.Title, nonEmpty(p.ID, p.StatusLabel, p.Headline))

	colPlayed := right - int(float64(inner)*0.30)
	colWin := right - int(float64(inner)*0.21)
	colLose := right - int(float64(inner)*0.12)

	rightText := func(text string, x, top int, f *Face, c color.RGBA) {
		DrawText(dst, x-int(f.Width(text)), top, text, f, c)
	}

	// ---- 小组赛：每组一张积分表 + 逐场比分 ----
	for _, group := range p.Groups {
		if y > MaxHeight-340 {
			break
		}
		title := group.Key
		if !hasSuffix(title, "组") {
			title += " 组"
		}
		y += SectionHeader(dst, pad, y, right, title, fSec, group.Note, fKey)
		if len(group.Rows) > 0 {
			DrawText(dst, pad+8, y, "#", fSmall, Mute)
			DrawText(dst, pad+56, y, "队伍", fSmall, Mute)
			rightText("场次", colPlayed, y, fSmall, Mute)
			rightText("胜", colWin, y, fSmall, Mute)
			rightText("负", colLose, y, fSmall, Mute)
			rightText("名次分", right, y, fSmall, Mute)
			y += smallH
			HLine(dst, pad, right, y-8, Line)
			for _, row := range group.Rows {
				name := Clip(row.Name, fBody, float64(int(float64(inner)*0.42)))
				DrawText(dst, pad+8, y, itoaOr(row.Rank, "—"), fBody, Accent)
				DrawText(dst, pad+56, y, name, fBody, Text)
				rightText(itoa(row.Played), colPlayed, y, fBody, Text)
				rightText(itoa(row.Win), colWin, y, fBody, Text)
				rightText(itoa(row.Lose), colLose, y, fBody, Text)
				rightText(itoa(row.Placement), right, y, fBody, Text)
				if row.Seed > 0 {
					x := pad + 56 + int(fBody.Width(name)) + 14
					DrawText(dst, x, y+4, "晋级 #"+itoa(row.Seed), fSmall, Amber)
				}
				y += rowH
			}
			y += 10
		}
		y = drawMatchLines(dst, pad, y, group.Matches, fSmall, fBody, fNote)
		y += 16
	}

	// ---- 积分制：均分榜 ----
	if len(p.Standings) > 0 {
		y += SectionHeader(dst, pad, y, right, "积分榜", fSec, "均分排名 · 满场次才参与名次", fKey)
		DrawText(dst, pad+8, y, "#", fSmall, Mute)
		DrawText(dst, pad+56, y, "队伍", fSmall, Mute)
		rightText("场次", colPlayed, y, fSmall, Mute)
		rightText("均分", colWin, y, fSmall, Mute)
		rightText("总分", right, y, fSmall, Mute)
		y += smallH
		HLine(dst, pad, right, y-8, Line)
		for _, row := range p.Standings {
			name := Clip(row.Name, fBody, float64(int(float64(inner)*0.5)))
			DrawText(dst, pad+8, y, itoaOr(row.Rank, "—"), fBody, Accent)
			DrawText(dst, pad+56, y, name, fBody, Text)
			rightText(itoa(row.Played), colPlayed, y, fBody, Text)
			rightText(row.Average, colWin, y, fBody, Text)
			rightText(itoa(row.Points), right, y, fBody, Accent)
			y += rowH
		}
		y += 16
	}

	if len(p.Matches) > 0 {
		y += SectionHeader(dst, pad, y, right, "逐局比分", fSec, "共 "+itoa(len(p.Matches))+" 局", fKey)
		y = drawMatchLines(dst, pad, y, p.Matches, fSmall, fBody, fNote)
		y += 16
	}

	// ---- 淘汰赛树状图（坐标在 payload 里算好，这里只负责画）----
	if p.Tree != nil {
		tree := p.Tree
		note := "双败淘汰 · 胜者组落败者进败者组"
		if tree.Single {
			note = "单败淘汰 · 输一场即淘汰"
		}
		y += SectionHeader(dst, pad, y, right, "淘汰赛", fSec, note, fKey)
		ox, oy := pad, y
		for _, band := range tree.Bands {
			if band.W > 0 && band.H > 0 {
				fillRect(dst, image.Rect(ox+band.X, oy+band.Y, ox+band.X+band.W, oy+band.Y+band.H),
					color.RGBA{Accent.R, Accent.G, Accent.B, 12})
			}
			DrawText(dst, ox+band.X+12, oy+band.TitleY, band.Title, fKey, Accent)
		}
		boxW, boxH, sideRow, champW := treeDims(tree)
		for _, col := range tree.Columns {
			DrawText(dst, ox+col.X, oy+col.Y, Clip(col.Title, fSmall, float64(boxW)), fSmall, Mute)
		}
		for _, link := range tree.Links {
			c := color.RGBA{Accent.R, Accent.G, Accent.B, 200}
			if !link.Done {
				c = color.RGBA{70, 92, 124, 200}
			}
			mx := ox + link.X2 - 20
			ThickLine(dst, ox+link.X1, oy+link.Y1, mx, oy+link.Y1, 2, c)
			ThickLine(dst, mx, oy+link.Y1, mx, oy+link.Y2, 2, c)
			ThickLine(dst, mx, oy+link.Y2, ox+link.X2, oy+link.Y2, 2, c)
		}
		for _, node := range tree.Nodes {
			nx, ny := ox+node.X, oy+node.Y
			nodeH := node.H
			if nodeH <= 0 {
				nodeH = boxH
			}
			live := node.Status == "live"
			edge := BoxEdge
			if live {
				edge = Amber
			}
			RoundedRect(dst, image.Rect(nx, ny, nx+boxW, ny+nodeH), 10, BoxFill, edge)
			DrawText(dst, nx+12, ny+7, node.Code, fCode, Mute)
			if node.StatusLabel != "" {
				tag := node.StatusLabel
				tc := Mute
				if live {
					tc = Amber
				}
				DrawText(dst, nx+boxW-12-int(fCode.Width(tag)), ny+7, tag, fCode, tc)
			}
			for i, side := range node.Sides {
				if i >= 4 {
					break
				}
				sy := ny + 30 + i*sideRow
				fillCircle(dst, float64(nx+18), float64(sy+11), 6, parseHexColor(side.Color, Accent))
				name := Clip(side.Name, fBox, float64(boxW-96))
				nameColor := Dim
				if side.Win || node.Status != "done" {
					nameColor = Text
				}
				DrawText(dst, nx+32, sy, name, fBox, nameColor)
				if side.Score != "" {
					sc := Dim
					if side.Win {
						sc = Accent
					}
					rightTextIn(dst, side.Score, nx+boxW-12, sy, fBox, sc)
				}
			}
		}
		champ := tree.Champ
		cx, cy := ox+champ.X, oy+champ.Y
		RoundedRect(dst, image.Rect(cx, cy, cx+champW, cy+boxH), 10, PanelHi, Accent)
		DrawText(dst, cx+14, cy+8, "CHAMPION", fCode, Accent)
		name := champ.Name
		nc := Text
		if name == "" {
			name = "—"
			nc = Dim
		}
		DrawText(dst, cx+14, cy+40, Clip(name, fBox, float64(champW-28)), fBox, nc)
		y = oy + tree.Height + 30
	}

	return footer(dst, width, y, p.Caption, site, "逐场结果与对阵自动生成", fNote)
}

// treeDims 取对阵框尺寸：优先用 payload 给的，缺了才退回兜底常量。
func treeDims(t *Tree) (boxW, boxH, sideRow, champW int) {
	boxW, boxH, sideRow, champW = treeBox, treeBoxH, treeSideRow, treeChampW
	if t.Box.W > 0 {
		boxW = t.Box.W
	}
	if t.Box.H > 0 {
		boxH = t.Box.H
	}
	if t.Box.SideRow > 0 {
		sideRow = t.Box.SideRow
	}
	if t.Box.ChampW > 0 {
		champW = t.Box.ChampW
	}
	return
}

func drawMatchLines(dst *image.RGBA, pad, y int, matches []MatchLine, fSmall, fBody, fNote *Face) int {
	for _, m := range matches {
		if y > MaxHeight-220 {
			break
		}
		DrawText(dst, pad, y, m.Round, fSmall, Mute)
		DrawText(dst, pad+int(fSmall.Width(m.Round))+14, y+2, m.Line, fBody, pickDone(m.Done, Text, Dim))
		y += int(float64(fBody.Size()) * 1.5)
	}
	_ = fNote
	return y
}

// footer 画页脚（细线 + 配文 + 站点 + 底部胶带），返回最终高度。
func footer(dst *image.RGBA, width, y int, caption, site, tagline string, f *Face) int {
	pad := PadFor(width)
	right := width - pad
	if y > MaxHeight-130 {
		y = MaxHeight - 130
	}
	HLine(dst, pad, right, y, Line)
	y += 24
	DrawText(dst, pad, y, caption, f, Dim)
	if site != "" {
		DrawText(dst, pad, y+32, site+" · "+tagline, f, Accent)
		y += 32
	}
	y += f.Size() + 26
	HazardTape(dst, 0, y, width, y+5)
	return y + 14
}

// --------------------------------------------------------------------------- //
// 小工具
// --------------------------------------------------------------------------- //
func nonEmpty(bits ...string) []string {
	out := make([]string, 0, len(bits))
	for _, b := range bits {
		if b != "" {
			out = append(out, b)
		}
	}
	return out
}

func trimSpace(s string) string {
	start, end := 0, len(s)
	for start < end && isSpace(s[start]) {
		start++
	}
	for end > start && isSpace(s[end-1]) {
		end--
	}
	return s[start:end]
}

func isSpace(b byte) bool { return b == ' ' || b == '\t' || b == '\n' || b == '\r' }

func hasSuffix(s, suffix string) bool {
	return len(s) >= len(suffix) && s[len(s)-len(suffix):] == suffix
}

func itoaOr(v int, fallback string) string {
	if v <= 0 {
		return fallback
	}
	return itoa(v)
}

func pickDone(done bool, yes, no color.RGBA) color.RGBA {
	if done {
		return yes
	}
	return no
}

// rightTextIn 以 x 为右边界画一行字（不需要右边界时用 DrawText）。
func rightTextIn(dst *image.RGBA, text string, x, top int, f *Face, c color.RGBA) {
	DrawText(dst, x-int(f.Width(text)), top, text, f, c)
}

// parseHexColor 解析 "#rrggbb" / "#rgb"；认不出来就退回给定颜色。
func parseHexColor(raw string, fallback color.RGBA) color.RGBA {
	s := raw
	if len(s) > 0 && s[0] == '#' {
		s = s[1:]
	}
	if len(s) == 3 {
		s = string([]byte{s[0], s[0], s[1], s[1], s[2], s[2]})
	}
	if len(s) != 6 {
		return fallback
	}
	hex := func(b byte) (int, bool) {
		switch {
		case b >= '0' && b <= '9':
			return int(b - '0'), true
		case b >= 'a' && b <= 'f':
			return int(b-'a') + 10, true
		case b >= 'A' && b <= 'F':
			return int(b-'A') + 10, true
		}
		return 0, false
	}
	var rgb [3]int
	for i := 0; i < 3; i++ {
		hi, ok1 := hex(s[i*2])
		lo, ok2 := hex(s[i*2+1])
		if !ok1 || !ok2 {
			return fallback
		}
		rgb[i] = hi*16 + lo
	}
	return color.RGBA{uint8(rgb[0]), uint8(rgb[1]), uint8(rgb[2]), 255}
}

var _ = math.Abs
