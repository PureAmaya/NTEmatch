package render

import (
	"image"
	"image/color"
	"image/draw"
	"math"
)

// --------------------------------------------------------------------------- //
// 像素级基础件
//
// 这里不引第三方绘图库：需要的形状只有「渐变条 / 斜纹胶带 / 网点 / 切角面板 /
// 方框 / 折线」六种，自己按像素画反而更可控——也省掉一层依赖。
// --------------------------------------------------------------------------- //

func clampByte(v float64) uint8 {
	if v <= 0 {
		return 0
	}
	if v >= 255 {
		return 255
	}
	return uint8(v + 0.5)
}

// blend 把 c（含 alpha）压到 dst 上。画布全程不透明，所以只算 RGB 就够。
func blend(dst *image.RGBA, x, y int, c color.RGBA) {
	if dst == nil {
		return
	}
	if x < dst.Rect.Min.X || x >= dst.Rect.Max.X || y < dst.Rect.Min.Y || y >= dst.Rect.Max.Y {
		return
	}
	a := c.A
	if a == 0 {
		return
	}
	i := dst.PixOffset(x, y)
	if a == 255 {
		dst.Pix[i], dst.Pix[i+1], dst.Pix[i+2], dst.Pix[i+3] = c.R, c.G, c.B, 255
		return
	}
	ia := float64(255 - a)
	af := float64(a)
	dst.Pix[i] = clampByte((float64(c.R)*af + float64(dst.Pix[i])*ia) / 255)
	dst.Pix[i+1] = clampByte((float64(c.G)*af + float64(dst.Pix[i+1])*ia) / 255)
	dst.Pix[i+2] = clampByte((float64(c.B)*af + float64(dst.Pix[i+2])*ia) / 255)
	dst.Pix[i+3] = 255
}

func fillRect(dst *image.RGBA, r image.Rectangle, c color.RGBA) {
	if dst == nil || r.Empty() {
		return
	}
	if c.A == 255 {
		draw.Draw(dst, r, &image.Uniform{C: c}, image.Point{}, draw.Over)
		return
	}
	// 半透明色不能直接交给 draw.Draw：color.RGBA 是**预乘**语义，把
	// {34,224,232,12} 当预乘色丢进去会被当成「加色」糊成一片纯青。
	// 这里按非预乘解读，逐行混合。
	for y := r.Min.Y; y < r.Max.Y; y++ {
		fillSpan(dst, y, float64(r.Min.X), float64(r.Max.X), c)
	}
}

// fillSpan 画一行横条，两端按小数部分做半透明边缘（等于 1px 抗锯齿）。
func fillSpan(dst *image.RGBA, y int, x0, x1 float64, c color.RGBA) {
	if x1 <= x0 {
		return
	}
	start := int(math.Floor(x0))
	end := int(math.Ceil(x1))
	for x := start; x < end; x++ {
		cov := math.Min(x1, float64(x+1)) - math.Max(x0, float64(x))
		if cov <= 0 {
			continue
		}
		cc := c
		cc.A = clampByte(float64(c.A) * cov)
		blend(dst, x, y, cc)
	}
}

func lerp(a, b color.RGBA, t float64) color.RGBA {
	return color.RGBA{
		R: clampByte(float64(a.R) + (float64(b.R)-float64(a.R))*t),
		G: clampByte(float64(a.G) + (float64(b.G)-float64(a.G))*t),
		B: clampByte(float64(a.B) + (float64(b.B)-float64(a.B))*t),
		A: clampByte(float64(a.A) + (float64(b.A)-float64(a.A))*t),
	}
}

// pickStop 在多段渐变里取 t（0..1）处的颜色。
func pickStop(stops []color.RGBA, t float64) color.RGBA {
	if len(stops) == 1 {
		return stops[0]
	}
	if t < 0 {
		t = 0
	}
	if t > 1 {
		t = 1
	}
	span := 1 / float64(len(stops)-1)
	idx := int(t / span)
	if idx > len(stops)-2 {
		idx = len(stops) - 2
	}
	return lerp(stops[idx], stops[idx+1], (t-float64(idx)*span)/span)
}

// rampImage 生成一张线性渐变图（水平或垂直），用来给条、块、标题上色。
func rampImage(w, h int, stops []color.RGBA, vertical bool) *image.RGBA {
	if w <= 0 || h <= 0 {
		w, h = 1, 1
	}
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	if vertical {
		for y := 0; y < h; y++ {
			c := pickStop(stops, float64(y)/math.Max(1, float64(h-1)))
			for x := 0; x < w; x++ {
				img.SetRGBA(x, y, c)
			}
		}
		return img
	}
	for x := 0; x < w; x++ {
		c := pickStop(stops, float64(x)/math.Max(1, float64(w-1)))
		for y := 0; y < h; y++ {
			img.SetRGBA(x, y, c)
		}
	}
	return img
}

// --------------------------------------------------------------------------- //
// 背景与装饰
// --------------------------------------------------------------------------- //
// NewCanvas 造一张底图：墨色底 + 顶部一层极淡的青→品红渐变。
//
// 只画 240px 高的一条带子，不是整张图跑渐变：一张 1080×6000 的底逐像素渐变要
// 六百万次混合，肉眼却看不出差别。
func NewCanvas(w, h int) *image.RGBA {
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	fillRect(img, img.Bounds(), BG)
	bandH := h
	if bandH > 240 {
		bandH = 240
	}
	ramp := rampImage(w, bandH, TitleStops, false)
	for y := 0; y < bandH; y++ {
		alpha := (1 - float64(y)/float64(bandH)) * 40
		for x := 0; x < w; x++ {
			c := ramp.RGBAAt(x, y)
			c.A = uint8(alpha)
			blend(img, x, y, c)
		}
	}
	return img
}

// GradientBar 画一条青→品红的细条（标题下 / 页脚上）。
func GradientBar(dst *image.RGBA, x, y, w, h int, stops []color.RGBA) {
	if dst == nil || w <= 0 || h <= 0 {
		return
	}
	draw.Draw(dst, image.Rect(x, y, x+w, y+h), rampImage(w, h, stops, false), image.Point{}, draw.Over)
}

// HazardTape 画警示胶带：青 / 黑 / 品红 / 黑 四段斜条，与网页顶栏下面那条同源。
func HazardTape(dst *image.RGBA, x0, y0, x1, y1 int) {
	if dst == nil {
		return
	}
	const period = 48.0
	colors := []color.RGBA{Accent, Ink, Accent2, Ink}
	for y := y0; y < y1; y++ {
		shift := float64(y-y0) * 0.55
		for x := x0; x < x1; x++ {
			pos := math.Mod(float64(x)-shift+period*4, period)
			idx := int(pos / (period / 4))
			if idx > 3 {
				idx = 3
			}
			dst.SetRGBA(x, y, colors[idx])
		}
	}
}

// Halftone 铺一片网点（印刷网点的观感），按距离角落淡出。
//
// fade：tr（右上角最密）/ bl（左下角最密）/ none。
func Halftone(dst *image.RGBA, r image.Rectangle, c color.RGBA, pitch, radius int, alpha uint8, fade string) {
	if dst == nil {
		return
	}
	if pitch < 4 {
		pitch = 4
	}
	w, h := r.Dx(), r.Dy()
	if w <= 0 || h <= 0 {
		return
	}
	for gy := 0; gy < h; gy += pitch {
		for gx := 0; gx < w; gx += pitch {
			var dist float64
			switch fade {
			case "tr":
				dist = float64(w-gx)/float64(w)*0.55 + float64(gy)/float64(h)*0.45
			case "bl":
				dist = float64(gx)/float64(w)*0.55 + float64(h-gy)/float64(h)*0.45
			}
			if dist > 1 {
				dist = 1
			}
			a := float64(alpha) * (1 - dist)
			if a <= 2 {
				continue
			}
			cc := c
			cc.A = uint8(a)
			fillCircle(dst, float64(r.Min.X+gx+pitch/2), float64(r.Min.Y+gy+pitch/2), float64(radius), cc)
		}
	}
}

// fillCircle 画一个抗锯齿实心圆（4×4 采样）。
func fillCircle(dst *image.RGBA, cx, cy, r float64, c color.RGBA) {
	if r <= 0 {
		return
	}
	x0 := int(math.Floor(cx - r - 1))
	x1 := int(math.Ceil(cx + r + 1))
	y0 := int(math.Floor(cy - r - 1))
	y1 := int(math.Ceil(cy + r + 1))
	const n = 4
	total := float64(n * n)
	for y := y0; y <= y1; y++ {
		for x := x0; x <= x1; x++ {
			hit := 0
			for sy := 0; sy < n; sy++ {
				for sx := 0; sx < n; sx++ {
					px := float64(x) + (float64(sx)+0.5)/n
					py := float64(y) + (float64(sy)+0.5)/n
					if (px-cx)*(px-cx)+(py-cy)*(py-cy) <= r*r {
						hit++
					}
				}
			}
			if hit == 0 {
				continue
			}
			cc := c
			cc.A = clampByte(float64(c.A) * float64(hit) / total)
			blend(dst, x, y, cc)
		}
	}
}

// panelRow 给出某一行在「左上切角 + 其余小圆角」形状里的左右边界（像素中心判断）。
func panelRow(y int, r image.Rectangle, cut, radius int) (float64, float64, bool) {
	yc := float64(y) + 0.5
	if yc < float64(r.Min.Y) || yc > float64(r.Max.Y) {
		return 0, 0, false
	}
	left := float64(r.Min.X)
	right := float64(r.Max.X)

	// 左上：45° 斜切
	if cut > 0 && yc < float64(r.Min.Y+cut) {
		left += float64(r.Min.Y+cut) - yc
	}
	if radius > 0 {
		if d := float64(r.Min.Y+radius) - yc; d > 0 { // 右上圆角
			right -= float64(radius) - math.Sqrt(math.Max(0, float64(radius*radius)-d*d))
		}
		if d := yc - float64(r.Max.Y-radius); d > 0 { // 右下圆角
			right -= float64(radius) - math.Sqrt(math.Max(0, float64(radius*radius)-d*d))
		}
		if d := yc - float64(r.Max.Y-radius); d > 0 { // 左下圆角
			left += float64(radius) - math.Sqrt(math.Max(0, float64(radius*radius)-d*d))
		}
	}
	return left, right, right > left
}

// Panel 画站点那种「左上切角 + 其余小圆角」的面板：填充 + 1px 淡青边 + 顶部高光。
func Panel(dst *image.RGBA, r image.Rectangle, cut, radius int, fill, edge color.RGBA) {
	if dst == nil {
		return
	}
	for y := r.Min.Y; y < r.Max.Y; y++ {
		l, rr, ok := panelRow(y, r, cut, radius)
		if !ok {
			continue
		}
		fillSpan(dst, y, l, rr, fill)
		blend(dst, int(l), y, edge)
		blend(dst, int(rr)-1, y, edge)
	}
	// 上下两条边（只在真的落在形状里的列上画）
	for x := r.Min.X + cut; x < r.Max.X-radius; x++ {
		blend(dst, x, r.Min.Y, edge)
	}
	for x := r.Min.X + radius; x < r.Max.X-radius; x++ {
		blend(dst, x, r.Max.Y-1, edge)
	}
	// 顶边一道极淡高光：给平面一点「受光」的暗示
	hi := color.RGBA{186, 224, 255, 16}
	for x := r.Min.X + cut + 2; x < r.Max.X-radius-2; x++ {
		blend(dst, x, r.Min.Y+1, hi)
	}
}

// RoundedRect 画圆角矩形（对阵框、冠军框）。
func RoundedRect(dst *image.RGBA, r image.Rectangle, radius int, fill, edge color.RGBA) {
	if dst == nil {
		return
	}
	for y := r.Min.Y; y < r.Max.Y; y++ {
		yc := float64(y) + 0.5
		left := float64(r.Min.X)
		right := float64(r.Max.X)
		if radius > 0 {
			if d := float64(r.Min.Y+radius) - yc; d > 0 {
				left += float64(radius) - math.Sqrt(math.Max(0, float64(radius*radius)-d*d))
				right -= float64(radius) - math.Sqrt(math.Max(0, float64(radius*radius)-d*d))
			}
			if d := yc - float64(r.Max.Y-radius); d > 0 {
				left += float64(radius) - math.Sqrt(math.Max(0, float64(radius*radius)-d*d))
				right -= float64(radius) - math.Sqrt(math.Max(0, float64(radius*radius)-d*d))
			}
		}
		fillSpan(dst, y, left, right, fill)
		blend(dst, int(left), y, edge)
		blend(dst, int(right)-1, y, edge)
	}
	for x := r.Min.X + radius; x < r.Max.X-radius; x++ {
		blend(dst, x, r.Min.Y, edge)
		blend(dst, x, r.Max.Y-1, edge)
	}
}

// ThickLine 画一条粗折线段（对阵图的连线）。
func ThickLine(dst *image.RGBA, x0, y0, x1, y1, width int, c color.RGBA) {
	if dst == nil {
		return
	}
	if width < 1 {
		width = 1
	}
	half := width / 2
	if y0 == y1 {
		if x1 < x0 {
			x0, x1 = x1, x0
		}
		for x := x0; x <= x1; x++ {
			for dy := 0; dy < width; dy++ {
				blend(dst, x, y0-half+dy, c)
			}
		}
		return
	}
	if x0 == x1 {
		if y1 < y0 {
			y0, y1 = y1, y0
		}
		for y := y0; y <= y1; y++ {
			for dx := 0; dx < width; dx++ {
				blend(dst, x0-half+dx, y, c)
			}
		}
		return
	}
	// 一般情况：按较长轴逐点插值（树状图里只有斜线段）
	steps := int(math.Max(math.Abs(float64(x1-x0)), math.Abs(float64(y1-y0))))
	for i := 0; i <= steps; i++ {
		t := float64(i) / float64(steps)
		x := int(math.Round(float64(x0) + float64(x1-x0)*t))
		y := int(math.Round(float64(y0) + float64(y1-y0)*t))
		for dy := 0; dy < width; dy++ {
			for dx := 0; dx < width; dx++ {
				blend(dst, x+dx-half, y+dy-half, c)
			}
		}
	}
}

// HLine 画一条 1px 横线。
func HLine(dst *image.RGBA, x0, x1, y int, c color.RGBA) {
	if dst == nil {
		return
	}
	if x1 < x0 {
		x0, x1 = x1, x0
	}
	for x := x0; x <= x1; x++ {
		blend(dst, x, y, c)
	}
}
