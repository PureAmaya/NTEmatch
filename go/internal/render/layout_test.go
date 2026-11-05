package render

import "testing"

// faceForTest 拿一个测试用的字面；这台机器上没有中文字体就跳过（出图本来就不可用）。
func faceForTest(t *testing.T, size int) *Face {
	t.Helper()
	f, err := FaceFor(size, false)
	if err != nil {
		t.Skipf("没有可用的中文字体：%v", err)
	}
	return f
}

// TestWrapNeverExceedsWidth 是排版最基本的一条：**一行不许宽过给定宽度**。
//
// 这条原先在 Python 侧靠「探针替换 ImageDraw.text」验证；排版搬到 Go 之后，
// 在折行这一层直接守住更准——折行错了，后面怎么画都会溢出。
func TestWrapNeverExceedsWidth(t *testing.T) {
	face := faceForTest(t, 26)
	texts := []string{
		"赛制：锦标赛制——每队 2 人，队友在组队时随机分配、全程固定（不换队、不换队友）。",
		"比赛 [届次] —— 信息 + 进度 / 谁在直播（主直播间 + 机位）",
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"短",
	}
	for _, width := range []float64{120, 240, 480, 952} {
		for _, text := range texts {
			for _, line := range Wrap(text, face, width) {
				if got := face.Width(line); got > width+0.5 {
					t.Fatalf("折行溢出：宽 %.0f，行 «%s» 实际 %.1f", width, line, got)
				}
			}
		}
	}
}

// TestClipFits 方框里没有滚动条：截断后的文字必须真的放得下。
func TestClipFits(t *testing.T) {
	face := faceForTest(t, 21)
	text := "雾巢先遣队 · 异象管理局 · 环城夜行 · 泊暮车队"
	for _, width := range []float64{40, 120, 250} {
		got := Clip(text, face, width)
		if w := face.Width(got); w > width+0.5 {
			t.Fatalf("截断后仍然放不下：宽 %.0f，结果 «%s» 实际 %.1f", width, got, w)
		}
	}
}

// TestPickWidthFollowsContent 取景规则：内容少用窄画布，内容多用宽画布。
func TestPickWidthFollowsContent(t *testing.T) {
	narrow, wide := Widths[0], Widths[len(Widths)-1]

	short := PickWidth(func(int) int { return 260 }, 0)
	if short != narrow {
		t.Fatalf("短内容应当用窄画布 %d，实际 %d", narrow, short)
	}
	tall := PickWidth(func(int) int { return 3000 }, 0)
	if tall != wide {
		t.Fatalf("长内容应当用宽画布 %d，实际 %d", wide, tall)
	}
	// 内容自身的下限（结果卡片的树状图）必须被尊重
	huge := PickWidth(func(int) int { return 1000 }, 1600)
	if huge != 1600 {
		t.Fatalf("应当按内容下限取 1600，实际 %d", huge)
	}
}

// TestRenderEventProducesImage 冒烟：真画一张，尺寸与上限都要站得住。
func TestRenderEventProducesImage(t *testing.T) {
	if err := LoadFonts(); err != nil {
		t.Skipf("没有可用的中文字体：%v", err)
	}
	img := RenderEvent("nte.example.com", EventPayload{
		Title:    "调试演示赛",
		ID:       "e001",
		Status:   "筹备中",
		Headline: "8 队 · 单败",
		Meta:     [][2]string{{"状态", "筹备中"}, {"赛制", "单败淘汰"}},
		Sections: []Section{{Title: "赛制概览", Items: []string{"淘汰赛：8 强 · 单败"}}},
		Caption:  "【调试演示赛】 · 完整赛制与规则见图",
	})
	if img == nil {
		t.Fatal("渲染返回空图")
	}
	b := img.Bounds()
	if b.Dx() != Widths[0] && b.Dx() != Widths[len(Widths)-1] {
		t.Fatalf("宽度不在阶梯里：%d", b.Dx())
	}
	if b.Dy() < 200 || b.Dy() > MaxHeight {
		t.Fatalf("高度不合理：%d", b.Dy())
	}
}
