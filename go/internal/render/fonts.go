package render

import (
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"golang.org/x/image/font"
	"golang.org/x/image/font/opentype"
	"golang.org/x/image/font/sfnt"
	"golang.org/x/image/math/fixed"
)

// Face 是一个「按像素字号实例化好」的字体，外加它是怎么来的。
type Face struct {
	face  font.Face
	size  int
	bold  bool
	synth bool // 没找到真正的粗体时，用「描两遍」凑出来
}

// Width 返回这段文字的排版宽度（像素）。
func (f *Face) Width(s string) float64 { return fixedToFloat(font.MeasureString(f.face, s)) }

// Ascent 是基线到字顶的距离（像素）。
func (f *Face) Ascent() int { return f.face.Metrics().Ascent.Round() }

// Size 是字号（像素）。
func (f *Face) Size() int { return f.size }

func fixedToFloat(v fixed.Int26_6) float64 { return float64(v) / 64 }

// --------------------------------------------------------------------------- //
// 字体发现
// --------------------------------------------------------------------------- //
// 出图必须有**中文**字体，而各发行版把它装在哪都不一样：
//   - Debian/Ubuntu：/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc（配 Bold.ttc）
//   - Arch：/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc
//   - NixOS：/run/current-system/sw/share/fonts/... 里的 SourceHanSans.ttc
//   - macOS：/System/Library/Fonts/PingFang.ttc
//   - Windows：C:\Windows\Fonts\msyh.ttc
//
// 注意：**变量字体（CFF2）解析不了**（golang.org/x/image 会报 invalid CFF table），
// 所以候选里遇到解析失败的直接跳过，继续找下一个——不能因为第一个文件在那儿就放弃。
var fontDirs = []string{
	"/usr/share/fonts",
	"/usr/local/share/fonts",
	"/run/current-system/sw/share/fonts",
	"/System/Library/Fonts",
	"/Library/Fonts",
	"/usr/share/fonts/opentype/noto",
}

// 名字里带这些的优先（同一台机器上往往有一堆语言字体，中文的那个才画得出汉字）。
var cjkHints = []string{"cjk", "han sans", "han serif", "source han", "noto sans sc", "noto serif sc",
	"pingfang", "hiragino", "yahei", "simhei", "simsun", "wqy", "droid sans fallback"}

type fontEntry struct {
	path   string
	index  int
	family string
	bold   bool
	score  int
	font   *sfnt.Font
	raw    []byte
}

var (
	fontOnce  sync.Once
	fontErr   error
	fontSet   []fontEntry
	faceCache sync.Map // string -> *Face
)

// FontError 表示这台机器上找不到可用的中文字体（出图不可用，调用方退回纯文本）。
var FontError = errors.New("没有找到可用的中文字体")

// LoadFonts 只做一次字体发现；之后都是内存判断。
func LoadFonts() error {
	fontOnce.Do(func() { fontErr = discoverFonts() })
	return fontErr
}

// FontPath 返回实际用到的字体文件（给 --version / 诊断用）。
func FontPath() string {
	if len(fontSet) == 0 {
		return ""
	}
	return fontSet[0].path
}

func discoverFonts() error {
	candidates := append([]string{}, envFontPaths()...)
	// fc-list 是 Linux / macOS 上最靠谱的入口：发行版把中文装在哪儿它都知道，
	// 也省得我们去扫整块盘（NixOS 的字体全在 /nix/store 下，扫不过来的）。
	candidates = append(candidates, fcListPaths()...)
	candidates = append(candidates, knownFontPaths()...)
	candidates = append(candidates, scanFontDirs()...)

	seen := map[string]bool{}
	var found []fontEntry
	for _, path := range candidates {
		if path == "" || seen[path] {
			continue
		}
		seen[path] = true
		entries := probeFontFile(path)
		found = append(found, entries...)
		if len(found) >= 24 { // 够挑了，不用把整台机器的字体全解析一遍
			break
		}
	}
	if len(found) == 0 {
		fontErr = fmt.Errorf("%w（装一个 Noto Sans CJK / 思源黑体即可）", FontError)
		return fontErr
	}
	sort.SliceStable(found, func(i, j int) bool { return found[i].score > found[j].score })
	fontSet = found
	return nil
}

func envFontPaths() []string {
	var out []string
	for _, key := range []string{"NTE_RENDER_FONT", "NTE_CJK_FONT"} {
		if v := strings.TrimSpace(os.Getenv(key)); v != "" {
			out = append(out, v)
		}
	}
	return out
}

// fcListPaths 问 fontconfig 要「能显示中文的字体文件」（没有 fc-list 就返回空）。
func fcListPaths() []string {
	binary, err := exec.LookPath("fc-list")
	if err != nil {
		return nil
	}
	out, err := exec.Command(binary, ":lang=zh", "file").Output()
	if err != nil {
		return nil
	}
	var paths []string
	for _, line := range strings.Split(string(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		// 输出形如 "/usr/share/fonts/.../NotoSansCJK-Regular.ttc: "
		if idx := strings.LastIndex(line, ": "); idx > 0 {
			line = line[:idx]
		}
		line = strings.TrimSuffix(line, ":")
		if line != "" {
			paths = append(paths, line)
		}
	}
	return paths
}

func knownFontPaths() []string {
	return []string{
		"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
		"/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
		"/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
		"/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
		"/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
		"/System/Library/Fonts/PingFang.ttc",
		"/System/Library/Fonts/STHeiti Medium.ttc",
		`C:\Windows\Fonts\msyh.ttc`,
		`C:\Windows\Fonts\msyhbd.ttc`,
		`C:\Windows\Fonts\simhei.ttf`,
	}
}

// scanFontDirs 扫常见字体目录（最多扫两层，够用且不至于把整块盘走一遍）。
func scanFontDirs() []string {
	var out []string
	for _, dir := range fontDirs {
		base, err := os.Stat(dir)
		if err != nil || !base.IsDir() {
			continue
		}
		_ = filepath.WalkDir(dir, func(path string, d os.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return nil
			}
			switch strings.ToLower(filepath.Ext(path)) {
			case ".ttc", ".ttf", ".otf":
				out = append(out, path)
			}
			return nil
		})
	}
	return out
}

// probeFontFile 解析一个字体文件，把它里面**能画中文**的字面都收下来。
func probeFontFile(path string) []fontEntry {
	info, err := os.Stat(path)
	if err != nil || info.Size() < 4096 || info.Size() > 80<<20 {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var fonts []*sfnt.Font
	if len(data) > 4 && string(data[:4]) == "ttcf" {
		coll, err := sfnt.ParseCollection(data)
		if err != nil {
			return nil
		}
		for i := 0; i < coll.NumFonts(); i++ {
			if f, err := coll.Font(i); err == nil {
				fonts = append(fonts, f)
			}
		}
	} else {
		f, err := sfnt.Parse(data)
		if err != nil {
			return nil
		}
		fonts = append(fonts, f)
	}

	var buf sfnt.Buffer
	var out []fontEntry
	for i, f := range fonts {
		// 画不出「赛」的一律不要——没有中文字形的字体会让整张图变成方框阵
		if idx, err := f.GlyphIndex(&buf, '赛'); err != nil || idx == 0 {
			continue
		}
		full, _ := f.Name(&buf, sfnt.NameIDFull)
		sub, _ := f.Name(&buf, sfnt.NameIDSubfamily)
		family, _ := f.Name(&buf, sfnt.NameIDFamily)
		label := strings.ToLower(full + " " + sub + " " + family)
		entry := fontEntry{
			path:   path,
			index:  i,
			family: family,
			bold:   isBoldName(label),
			font:   f,
			raw:    data,
		}
		entry.score = scoreName(label) + weightScore(label)
		out = append(out, entry)
	}
	// 同一个文件里同一家族只留分数最高的那个字面，避免 40 多个字面互相挤占名额
	return dedupeByFamily(out)
}

func dedupeByFamily(entries []fontEntry) []fontEntry {
	best := map[string]fontEntry{}
	var order []string
	for _, e := range entries {
		key := e.family + fmt.Sprint(e.bold)
		cur, ok := best[key]
		if !ok {
			order = append(order, key)
			best[key] = e
			continue
		}
		if e.score > cur.score {
			best[key] = e
		}
	}
	out := make([]fontEntry, 0, len(order))
	for _, key := range order {
		out = append(out, best[key])
	}
	return out
}

func isBoldName(label string) bool {
	for _, word := range []string{"bold", "heavy", "black", "semibold", "demibold", "medium"} {
		if strings.Contains(label, word) {
			return true
		}
	}
	return false
}

func scoreName(label string) int {
	score := 0
	for rank, hint := range cjkHints {
		if strings.Contains(label, hint) {
			score += 40 - rank*3
			break
		}
	}
	if strings.Contains(label, "sc") || strings.Contains(label, "gb") {
		score += 6
	}
	return score
}

func weightScore(label string) int {
	switch {
	case strings.Contains(label, "regular"), strings.Contains(label, "normal"):
		return 4
	case isBoldName(label):
		return 3
	}
	return 1
}

// --------------------------------------------------------------------------- //
// 取字面
// --------------------------------------------------------------------------- //
func pickFont(bold bool) *fontEntry {
	var fallback *fontEntry
	for i := range fontSet {
		e := &fontSet[i]
		if e.bold != bold {
			continue
		}
		if fallback == nil {
			fallback = e
		}
	}
	if fallback == nil && bold {
		return nil
	}
	return fallback
}

// FaceFor 取一个指定字号的字面（按字号缓存：opentype.NewFace 不便宜）。
func FaceFor(size int, bold bool) (*Face, error) {
	if err := LoadFonts(); err != nil {
		return nil, err
	}
	key := fmt.Sprintf("%d|%v", size, bold)
	if cached, ok := faceCache.Load(key); ok {
		return cached.(*Face), nil
	}
	entry := pickFont(bold)
	synth := false
	if entry == nil {
		entry = pickFont(false)
		synth = true // 没有粗体：用「描两遍」凑，别退回细体又什么都不做
	}
	if entry == nil {
		return nil, FontError
	}
	f, err := opentype.NewFace(entry.font, &opentype.FaceOptions{
		Size:    float64(size),
		DPI:     72,
		Hinting: font.HintingNone,
	})
	if err != nil {
		return nil, err
	}
	face := &Face{face: f, size: size, bold: bold, synth: synth}
	faceCache.Store(key, face)
	return face, nil
}

// FitFace 挑一个「一行装得下」的字号；都装不下就用最小的（随后仍会折行）。
func FitFace(text string, maxWidth int, sizes []int, bold bool) *Face {
	var smallest *Face
	for _, size := range sizes {
		face, err := FaceFor(size, bold)
		if err != nil {
			continue
		}
		if smallest == nil {
			smallest = face
		}
		if face.Width(text) <= float64(maxWidth) {
			return face
		}
	}
	if smallest == nil {
		panic("render: 没有可用字体")
	}
	return smallest
}

// --------------------------------------------------------------------------- //
// 画字
// --------------------------------------------------------------------------- //
// DrawText 以 (x, top) 为左上角画一行字。返回它占的宽度。
//
// top 指**字顶**而不是基线：排版代码里全是「这块内容多高」，用基线算会到处差一个 ascent。
func DrawText(dst *image.RGBA, x, top int, s string, f *Face, c color.RGBA) float64 {
	if dst == nil || s == "" || f == nil {
		return 0
	}
	baseline := top + f.Ascent()
	d := font.Drawer{Dst: dst, Src: image.NewUniform(c), Face: f.face, Dot: fixed.P(x, baseline)}
	d.DrawString(s)
	if f.synth {
		// 假粗体：同一行再压一遍（偏 0.6px），比退回细体更接近设计里的字重
		d2 := font.Drawer{Dst: dst, Src: image.NewUniform(c), Face: f.face,
			Dot: fixed.Point26_6{X: fixed.I(x) + fixed.Int26_6(38), Y: fixed.I(baseline)}}
		d2.DrawString(s)
	}
	return f.Width(s)
}

// GradientText 画渐变填充的文字（只给标题用）。返回它占的宽高。
//
// 做法：先把字画进一张灰度蒙版，再用蒙版把渐变贴上去——这样字与渐变都不会脏。
func GradientText(dst *image.RGBA, x, top int, s string, f *Face, stops []color.RGBA) (int, int) {
	if dst == nil || s == "" || f == nil {
		return 0, 0
	}
	b, _ := font.BoundString(f.face, s)
	w := (b.Max.X - b.Min.X).Ceil()
	h := (b.Max.Y - b.Min.Y).Ceil()
	if w <= 0 || h <= 0 {
		return 0, 0
	}
	mask := image.NewAlpha(image.Rect(0, 0, w, h))
	d := font.Drawer{
		Dst:  mask,
		Src:  image.NewUniform(color.Alpha{A: 255}),
		Face: f.face,
		Dot:  fixed.Point26_6{X: -b.Min.X, Y: -b.Min.Y},
	}
	d.DrawString(s)
	ramp := rampImage(w, h, stops, false)
	draw.DrawMask(dst, image.Rect(x, top, x+w, top+h), ramp, image.Point{}, mask, image.Point{}, draw.Over)
	return w, h
}
