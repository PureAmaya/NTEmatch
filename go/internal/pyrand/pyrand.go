// Package pyrand 复刻 CPython「random.Random(seed)」的随机数序列。
//
// 为什么要复刻而不是用 math/rand：赛程生成的搜索结果直接取决于随机序列。
// 若 Go 侧用自己的 PRNG，同一份配置、同一个 seed 在 Python 与 Go 下会生成
// 不同的赛程——迁移时用户看到「改了个后端，赛程全变了」，而且无法做逐字节对拍。
// 复刻 CPython（_randommodule.c）的 MT19937 + init_by_array + 取数规则后，
// 两边输出完全一致，就能用严格相等做回归测试（见 calc 包的测试）。
//
// 复刻范围只覆盖赛程搜索实际用到的那几个入口：
//   - seed(int)：绝对值的 32 位小端字数组 → init_by_array
//   - random()：两次 genrand_uint32 拼 53 位
//   - shuffle()：逆序 + _randbelow(getrandbits)
package pyrand

import (
	"math/bits"
)

const (
	mtN = 624
	mtM = 397
)

// Random 与 CPython random.Random 的整数 seed 分支等价。
//
// 非并发安全：每个搜索分支各建一个实例（与 Python 的「每 restart 一个 rng」一致）。
type Random struct {
	mt    [mtN]uint32
	index int
}

// New 等价于 Python 的 random.Random(seed)。
//
// Python 对负数 seed 取绝对值（PyNumber_Absolute），这里保持一致。
func New(seed int64) *Random {
	r := &Random{}
	r.initByArray(seedKey(seed))
	return r
}

// seedKey 把 seed 拆成「绝对值的 32 位小端字数组」，至少一个字。
// 对应 CPython random_seed 里的 keyused = bits == 0 ? 1 : (bits-1)/32 + 1。
func seedKey(seed int64) []uint32 {
	var n uint64
	if seed < 0 {
		// 先 +1 再取反，避免 math.MinInt64 溢出
		n = uint64(-(seed + 1)) + 1
	} else {
		n = uint64(seed)
	}
	if n == 0 {
		return []uint32{0}
	}
	key := make([]uint32, 0, 2)
	for n > 0 {
		key = append(key, uint32(n&0xffffffff))
		n >>= 32
	}
	return key
}

func (r *Random) initGenrand(seed uint32) {
	r.mt[0] = seed
	for i := 1; i < mtN; i++ {
		r.mt[i] = 1812433253*(r.mt[i-1]^(r.mt[i-1]>>30)) + uint32(i)
	}
	r.index = mtN
}

// initByArray 对应经典 MT19937 的 init_by_array（CPython 同款）。
func (r *Random) initByArray(key []uint32) {
	r.initGenrand(19650218)
	i, j := 1, 0
	k := mtN
	if len(key) > k {
		k = len(key)
	}
	for ; k > 0; k-- {
		r.mt[i] = (r.mt[i] ^ ((r.mt[i-1] ^ (r.mt[i-1] >> 30)) * 1664525)) + key[j] + uint32(j)
		i++
		j++
		if i >= mtN {
			r.mt[0] = r.mt[mtN-1]
			i = 1
		}
		if j >= len(key) {
			j = 0
		}
	}
	for k = mtN - 1; k > 0; k-- {
		r.mt[i] = (r.mt[i] ^ ((r.mt[i-1] ^ (r.mt[i-1] >> 30)) * 1566083941)) - uint32(i)
		i++
		if i >= mtN {
			r.mt[0] = r.mt[mtN-1]
			i = 1
		}
	}
	r.mt[0] = 0x80000000
	r.index = mtN
}

// Uint32 等价于 CPython 的内部 genrand_uint32。
func (r *Random) Uint32() uint32 {
	if r.index >= mtN {
		for i := 0; i < mtN; i++ {
			y := (r.mt[i] & 0x80000000) | (r.mt[(i+1)%mtN] & 0x7fffffff)
			r.mt[i] = r.mt[(i+mtM)%mtN] ^ (y >> 1)
			if y&1 != 0 {
				r.mt[i] ^= 0x9908b0df
			}
		}
		r.index = 0
	}
	y := r.mt[r.index]
	r.index++
	y ^= y >> 11
	y ^= (y << 7) & 0x9d2c5680
	y ^= (y << 15) & 0xefc60000
	y ^= y >> 18
	return y
}

// Float64 等价于 random.Random.random()。
//
// CPython：(a*67108864.0+b) * (1.0/9007199254740992.0)，a、b 各取一次 32 位输出。
// 浮点运算顺序必须照抄，否则末位可能不同（赛程代价的比较是精确比较）。
func (r *Random) Float64() float64 {
	a := uint64(r.Uint32() >> 5)
	b := uint64(r.Uint32() >> 6)
	return (float64(a)*67108864.0 + float64(b)) * (1.0 / 9007199254740992.0)
}

// getRandBits 只在 0 < k <= 32 时被 _randbelow 调用（与 CPython 的快速分支一致）。
func (r *Random) getRandBits(k uint) uint32 {
	if k == 0 {
		return 0
	}
	return r.Uint32() >> (32 - k)
}

// RandBelow 等价于 CPython 的 _randbelow_with_getrandbits。
func (r *Random) RandBelow(n int) int {
	if n <= 0 {
		return 0
	}
	k := uint(bits.Len(uint(n))) // n.bit_length()
	v := r.getRandBits(k)
	for int(v) >= n {
		v = r.getRandBits(k)
	}
	return int(v)
}

// Shuffle 等价于 random.Random.shuffle（逆序遍历 + 交换）。
func (r *Random) Shuffle(n int, swap func(i, j int)) {
	for i := n; i > 1; i-- {
		j := r.RandBelow(i)
		swap(i-1, j)
	}
}
