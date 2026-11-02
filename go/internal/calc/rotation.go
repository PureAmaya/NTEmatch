// Package calc 复刻积分制「动态轮换赛程」的多起点随机搜索。
//
// 语义对照：app/league.py 的 generate_rotation_rounds / _build_rotation_rounds。
// Python 侧是**权威定义**（也是 Go 不可用时的回退实现），本包只负责把同一套
// 搜索跑得更快；任何一处权重、顺序、浮点累加次序的改变都会让两边结果分叉，
// 因此 tests/test_gocalc.py 用逐局相等做对拍。
//
// 只返回「决策结果」（每局 A/B 两方的选手 id 与轮空名单），不生成 Round：
// 编号、标题、note 文案仍由 app/league.py 的 _make_round 统一产出，
// 避免同一份展示文案出现两处定义。
package calc

import (
	"errors"
	"fmt"
	"sort"
	"sync"

	"ntematch/ntecalc/internal/pyrand"
)

// 启发式权重：必须与 app/league.py 顶部的 _W_* 逐项相同。
const (
	weightRepeatPartner  = 14.0 // 重复搭档（2v2 轮换最需要避免）
	weightRepeatGroup    = 22.0 // 完全相同的四人组合重复
	weightRepeatOpponent = 3.0  // 重复对手
	weightRestStreak     = 9.0  // 连续轮空
	weightAppearance     = 1.2  // 出场次数不均衡

	restartCount    = 12  // 多起点搜索次数
	trialsPerRound  = 200 // 每局候选名单抽样次数
	defaultSeedBase = 20261003
	epsilon         = 1e-9 // 「更优」判定：严格优于，避免浮点抖动改写已有选择
)

// Player 是搜索所需的选手最小投影。
type Player struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// RotationRound 是一局的决策结果，选手顺序即上场顺序。
type RotationRound struct {
	A       []string `json:"a"`
	B       []string `json:"b"`
	Resting []string `json:"resting"`
}

// RotationResult 是整份赛程的决策结果。
type RotationResult struct {
	Rounds   []RotationRound `json:"rounds"`
	Cost     float64         `json:"cost"`
	Restarts int             `json:"restarts"`
}

// Errors 供调用方区分「配置不合法」与内部故障：前者不该重试，直接让 Python 侧报错。
var ErrBadInput = errors.New("bad input")

// history 对应 Python 侧的 hist: dict[str, Counter]。
type history struct {
	partner  map[string]int
	opponent map[string]int
	group    map[string]int
	rest     map[string]int
	appear   map[string]int
}

func newHistory() *history {
	return &history{
		partner:  make(map[string]int),
		opponent: make(map[string]int),
		group:    make(map[string]int),
		rest:     make(map[string]int),
		appear:   make(map[string]int),
	}
}

// pairKey 把两人组合规范化成唯一的 map 键（与 Python 的 tuple(sorted(...)) 等价）。
//
// 分隔符用 NUL：调用方在入口已拒绝含 NUL 的 id，因此不会撞键。
func pairKey(a, b string) string {
	if a > b {
		a, b = b, a
	}
	return a + "\x00" + b
}

// groupKey 把一组 id 规范化（排序后拼接），对应 tuple(sorted(ids))。
func groupKey(ids []string) string {
	sorted := append([]string(nil), ids...)
	sort.Strings(sorted)
	key := ""
	for i, id := range sorted {
		if i > 0 {
			key += "\x00"
		}
		key += id
	}
	return key
}

func idsOf(players []Player) []string {
	ids := make([]string, len(players))
	for i, p := range players {
		ids[i] = p.ID
	}
	return ids
}

func concat(a, b []Player) []Player {
	out := make([]Player, 0, len(a)+len(b))
	out = append(out, a...)
	out = append(out, b...)
	return out
}

// partnerKeys 对应 _partner_keys：先排序再枚举所有 i<j 对。
func partnerKeys(ids []string) []string {
	sorted := append([]string(nil), ids...)
	sort.Strings(sorted)
	keys := make([]string, 0, len(sorted)*(len(sorted)-1)/2)
	for i := 0; i < len(sorted); i++ {
		for j := i + 1; j < len(sorted); j++ {
			keys = append(keys, sorted[i]+"\x00"+sorted[j])
		}
	}
	return keys
}

// opponentKeys 对应 _opponent_keys：外层按 a 的顺序、内层按 b 的顺序，对内规范化。
func opponentKeys(aIDs, bIDs []string) []string {
	keys := make([]string, 0, len(aIDs)*len(bIDs))
	for _, x := range aIDs {
		for _, y := range bIDs {
			keys = append(keys, pairKey(x, y))
		}
	}
	return keys
}

// splitCost 对应 _split_cost。浮点累加次序必须与 Python 一致：
// 先 A 方搭档、再 B 方搭档、再对手、最后整场组合。
func splitCost(sideA, sideB []Player, key string, h *history) float64 {
	cost := 0.0
	for _, group := range [][]Player{sideA, sideB} {
		for _, k := range partnerKeys(idsOf(group)) {
			cost += float64(h.partner[k]) * weightRepeatPartner
		}
	}
	for _, k := range opponentKeys(idsOf(sideA), idsOf(sideB)) {
		cost += float64(h.opponent[k]) * weightRepeatOpponent
	}
	cost += float64(h.group[key]) * weightRepeatGroup
	return cost
}

// bestSplit 对应 _best_split：固定第一个 id 在 A 方，按 itertools.combinations
// 的字典序枚举其余组合，取代价最小者（严格更优才替换）。
func bestSplit(picked []Player, teamSize int, h *history) ([]Player, []Player, bool) {
	ids := idsOf(picked)
	key := groupKey(ids)
	anchor := ids[0]
	rest := ids[1:]
	pick := teamSize - 1
	if pick < 0 || pick > len(rest) {
		return nil, nil, false
	}

	var (
		bestA, bestB []Player
		bestCost     float64
		have         bool
	)
	combo := make([]int, pick)
	inA := make(map[string]struct{}, teamSize)

	var walk func(start, depth int)
	walk = func(start, depth int) {
		if depth == pick {
			for k := range inA {
				delete(inA, k)
			}
			inA[anchor] = struct{}{}
			for _, idx := range combo {
				inA[rest[idx]] = struct{}{}
			}
			a := make([]Player, 0, teamSize)
			b := make([]Player, 0, teamSize)
			for _, p := range picked {
				if _, ok := inA[p.ID]; ok {
					a = append(a, p)
				} else {
					b = append(b, p)
				}
			}
			if len(b) != teamSize {
				return
			}
			cost := splitCost(a, b, key, h)
			if !have || cost < bestCost-epsilon {
				have = true
				bestCost = cost
				bestA, bestB = a, b
			}
			return
		}
		// 组合字典序：第 depth 位从 start 起，且给后面的位留够元素
		for i := start; i <= len(rest)-(pick-depth); i++ {
			combo[depth] = i
			walk(i+1, depth+1)
		}
	}
	walk(0, 0)
	return bestA, bestB, have
}

// weightedSample 对应 _weighted_sample：按权重不放回抽样。
//
// 关键细节：每抽一次都重新累加 total（Python 的 sum(wts) 就在循环里），
// 且 rng 只取 random()——次序错一位，整份赛程就变了。
func weightedSample(pool []Player, weights []float64, count int, rng *pyrand.Random) []Player {
	items := append([]Player(nil), pool...)
	wts := append([]float64(nil), weights...)
	limit := count
	if len(items) < limit {
		limit = len(items)
	}
	picked := make([]Player, 0, limit)
	for n := 0; n < limit; n++ {
		total := 0.0
		for _, w := range wts {
			total += w
		}
		target := rng.Float64() * total
		idx := len(items) - 1
		acc := 0.0
		for i, w := range wts {
			acc += w
			if acc >= target {
				idx = i
				break
			}
		}
		picked = append(picked, items[idx])
		items = append(items[:idx], items[idx+1:]...)
		wts = append(wts[:idx], wts[idx+1:]...)
	}
	return picked
}

// buildRotation 对应 _build_rotation_rounds：逐局贪心，返回决策与总代价。
func buildRotation(players []Player, teamSize, totalRounds int, rng *pyrand.Random, fair bool) ([]RotationRound, float64, error) {
	need := teamSize * 2
	base := append([]Player(nil), players...)
	rng.Shuffle(len(base), func(i, j int) { base[i], base[j] = base[j], base[i] })

	h := newHistory()
	rounds := make([]RotationRound, 0, totalRounds)
	totalCost := 0.0
	trials := 1
	if len(base) > need {
		trials = trialsPerRound
	}

	for r := 0; r < totalRounds; r++ {
		var (
			bestCost     float64
			bestA, bestB []Player
			bestRest     []Player
			have         bool
		)

		consider := func(picked []Player) error {
			a, b, ok := bestSplit(picked, teamSize, h)
			if !ok {
				return fmt.Errorf("%w: 单场 %d 人无法分出两队", ErrBadInput, len(picked))
			}
			pickedIDs := make(map[string]struct{}, len(picked))
			for _, p := range picked {
				pickedIDs[p.ID] = struct{}{}
			}
			resting := make([]Player, 0, len(base)-len(picked))
			for _, p := range base {
				if _, ok := pickedIDs[p.ID]; !ok {
					resting = append(resting, p)
				}
			}
			cost := splitCost(a, b, groupKey(idsOf(picked)), h)
			if fair {
				for _, p := range resting {
					cost += float64(h.rest[p.ID]) * weightRestStreak
				}
				for _, p := range concat(a, b) {
					cost += float64(h.appear[p.ID]) * weightAppearance
				}
			}
			if !have || cost < bestCost-epsilon {
				have = true
				bestCost = cost
				bestA, bestB, bestRest = a, b, resting
			}
			return nil
		}

		if len(base) <= need {
			if err := consider(base); err != nil {
				return nil, 0, err
			}
		} else {
			for t := 0; t < trials; t++ {
				weights := make([]float64, len(base))
				for i, p := range base {
					w := 1.0 / (1.0 + 0.6*float64(h.appear[p.ID]))
					if fair {
						w += 0.5 * float64(h.rest[p.ID])
					}
					if w < 0.02 {
						w = 0.02
					}
					weights[i] = w
				}
				if err := consider(weightedSample(base, weights, need, rng)); err != nil {
					return nil, 0, err
				}
			}
		}
		if !have {
			return nil, 0, fmt.Errorf("%w: 第 %d 局没有可用候选", ErrBadInput, r+1)
		}
		totalCost += bestCost

		for _, group := range [][]Player{bestA, bestB} {
			for _, k := range partnerKeys(idsOf(group)) {
				h.partner[k]++
			}
		}
		for _, k := range opponentKeys(idsOf(bestA), idsOf(bestB)) {
			h.opponent[k]++
		}
		both := concat(bestA, bestB)
		h.group[groupKey(idsOf(both))]++
		for _, p := range both {
			h.appear[p.ID]++
			h.rest[p.ID] = 0
		}
		for _, p := range bestRest {
			h.rest[p.ID]++
		}

		rounds = append(rounds, RotationRound{
			A:       idsOf(bestA),
			B:       idsOf(bestB),
			Resting: idsOf(bestRest),
		})
	}
	return rounds, totalCost, nil
}

// GenerateRotation 对应 generate_rotation_rounds：多起点搜索取总代价最小的一份。
//
// 12 个 restart 彼此独立（各自的 rng 与 history），因此并行跑；
// 合并时严格按 restart 序号比较，保证与 Python 的串行顺序完全一致。
func GenerateRotation(players []Player, teamSize, totalRounds int, seed *int64, fair bool) (*RotationResult, error) {
	if teamSize < 1 {
		return nil, fmt.Errorf("%w: teamSize=%d", ErrBadInput, teamSize)
	}
	if totalRounds < 1 || totalRounds > 50 {
		return nil, fmt.Errorf("%w: totalRounds=%d", ErrBadInput, totalRounds)
	}
	need := teamSize * 2
	if len(players) < need {
		return nil, fmt.Errorf("%w: 可用选手 %d 人，少于单场所需的 %d 人", ErrBadInput, len(players), need)
	}
	seen := make(map[string]struct{}, len(players))
	for _, p := range players {
		if p.ID == "" {
			return nil, fmt.Errorf("%w: 选手 id 为空", ErrBadInput)
		}
		if hasNUL(p.ID) {
			return nil, fmt.Errorf("%w: 选手 id 含控制字符", ErrBadInput)
		}
		if _, dup := seen[p.ID]; dup {
			return nil, fmt.Errorf("%w: 选手 id 重复：%s", ErrBadInput, p.ID)
		}
		seen[p.ID] = struct{}{}
	}

	baseSeed := int64(defaultSeedBase)
	if seed != nil {
		baseSeed = *seed
	}

	type runResult struct {
		rounds []RotationRound
		cost   float64
		err    error
	}
	results := make([]runResult, restartCount)
	var wg sync.WaitGroup
	for i := 0; i < restartCount; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			rng := pyrand.New(baseSeed + int64(i)*7919)
			rounds, cost, err := buildRotation(players, teamSize, totalRounds, rng, fair)
			results[i] = runResult{rounds: rounds, cost: cost, err: err}
		}(i)
	}
	wg.Wait()

	bestIdx := -1
	for i, res := range results {
		if res.err != nil {
			return nil, res.err
		}
		if bestIdx < 0 || res.cost < results[bestIdx].cost-epsilon {
			bestIdx = i
		}
	}
	return &RotationResult{
		Rounds:   results[bestIdx].rounds,
		Cost:     results[bestIdx].cost,
		Restarts: restartCount,
	}, nil
}

func hasNUL(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] == 0 {
			return true
		}
	}
	return false
}
