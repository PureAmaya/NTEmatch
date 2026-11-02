package pyrand

import (
	"math"
	"testing"
)

// 期望值由项目虚拟环境里的 CPython 生成（random.Random(seed)），
// 是「Go 侧是否真的复刻了 Python」的唯一判据。
var floatCases = []struct {
	seed int64
	want []float64
}{
	{0, []float64{0.8444218515250481, 0.7579544029403025, 0.420571580830845, 0.25891675029296335, 0.5112747213686085}},
	{1, []float64{0.13436424411240122, 0.8474337369372327, 0.763774618976614, 0.2550690257394217, 0.49543508709194095}},
	{20261003, []float64{0.028164495600748074, 0.9217604846129798, 0.33883843507066314, 0.1434055107959067, 0.49778832710119914}},
	{20268922, []float64{0.42793038737264333, 0.6761157370597989, 0.9934080223038924, 0.8049089148164501, 0.4576082820395003}},
	{-5, []float64{0.6229016948897019, 0.7417869892607294, 0.7951935655656966, 0.9424502837770503, 0.7398985747399307}},
	{1099511640121, []float64{0.31036222241287226, 0.1148930862836336, 0.5335990945925775, 0.20403672857712885, 0.8113406885080037}},
}

func TestFloat64MatchesPython(t *testing.T) {
	for _, tc := range floatCases {
		r := New(tc.seed)
		for i, want := range tc.want {
			got := r.Float64()
			if math.Float64bits(got) != math.Float64bits(want) {
				t.Fatalf("seed=%d 第 %d 个 random() 不一致：got=%v want=%v", tc.seed, i, got, want)
			}
		}
	}
}

func TestShuffleMatchesPython(t *testing.T) {
	cases := []struct {
		seed int64
		want string
	}{
		{20261003, "fgdbchea"},
		{7, "ghceadbf"},
	}
	for _, tc := range cases {
		items := []byte("abcdefgh")
		r := New(tc.seed)
		r.Shuffle(len(items), func(i, j int) { items[i], items[j] = items[j], items[i] })
		if string(items) != tc.want {
			t.Fatalf("seed=%d shuffle 不一致：got=%s want=%s", tc.seed, string(items), tc.want)
		}
	}
}
