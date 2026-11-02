// Command ntecalc 是 NTE 比赛平台的计算侧车（sidecar）。
//
// 由 Python 主进程按需拉起，常驻，stdin/stdout 走逐行 JSON，
// 目前只承担积分制「动态轮换赛程」的多起点随机搜索——
// 这是全站唯一「CPU 密集 + 持全局写锁 + 跑在事件循环里」的操作。
//
// 约定：
//   - stdout 只输出协议帧，一个请求一行、绝不多余；
//   - 所有诊断信息走 stderr，Python 侧会转进自己的日志；
//   - stdin 读到 EOF 就退出（主进程没了，不该留孤儿）。
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"runtime"
	"sync"
	"time"

	"ntematch/ntecalc/internal/calc"
	"ntematch/ntecalc/internal/protocol"
)

// buildTag 在编译期用 -ldflags "-X main.buildTag=..." 覆盖，便于现场核对版本。
var buildTag = "dev"

func main() {
	log.SetOutput(os.Stderr)
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.SetPrefix("ntecalc ")

	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "--version":
			fmt.Printf("ntecalc %d %s\n", protocol.Version, buildTag)
			return
		case "--selftest":
			if err := selftest(); err != nil {
				fmt.Fprintf(os.Stderr, "selftest failed: %v\n", err)
				os.Exit(1)
			}
			fmt.Println("selftest ok")
			return
		default:
			fmt.Fprintf(os.Stderr, "unknown flag: %s\n", os.Args[1])
			os.Exit(2)
		}
	}

	log.Printf(
		"启动 | protocol=%d | tag=%s | go=%s | gomaxprocs=%d | pid=%d",
		protocol.Version, buildTag, runtime.Version(), runtime.GOMAXPROCS(0), os.Getpid(),
	)

	in := bufio.NewScanner(os.Stdin)
	// 一帧的规模上限：一届几十人 + 几十局，1MB 已经非常宽松。
	in.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)

	out := bufio.NewWriterSize(os.Stdout, 64*1024)
	var writeMu sync.Mutex
	var wg sync.WaitGroup

	for in.Scan() {
		// Scanner 会复用底层缓冲，必须复制一份再交给 goroutine。
		line := append([]byte(nil), in.Bytes()...)
		if len(line) == 0 {
			continue
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp := handle(line)
			writeMu.Lock()
			defer writeMu.Unlock()
			enc := json.NewEncoder(out)
			if err := enc.Encode(resp); err != nil {
				log.Printf("写出响应失败 | id=%d | err=%v", resp.ID, err)
				return
			}
			if err := out.Flush(); err != nil {
				log.Printf("刷新输出失败 | id=%d | err=%v", resp.ID, err)
			}
		}()
	}
	wg.Wait()
	if err := in.Err(); err != nil {
		log.Printf("读取 stdin 失败 | err=%v", err)
		os.Exit(1)
	}
	log.Printf("stdin 已关闭，退出")
}

func handle(line []byte) protocol.Response {
	started := time.Now()
	var req protocol.Request
	if err := json.Unmarshal(line, &req); err != nil {
		return protocol.Response{ID: req.ID, OK: false, Error: "请求不是合法 JSON: " + err.Error()}
	}
	resp := protocol.Response{ID: req.ID}
	data, err := dispatch(req)
	if err != nil {
		resp.OK = false
		resp.Error = err.Error()
		log.Printf("请求失败 | id=%d | op=%s | err=%v", req.ID, req.Op, err)
	} else {
		resp.OK = true
		resp.Data = data
	}
	resp.MS = float64(time.Since(started).Microseconds()) / 1000.0
	return resp
}

func dispatch(req protocol.Request) (any, error) {
	switch req.Op {
	case protocol.OpLeagueRotation:
		return leagueRotation(req.Args)
	default:
		return nil, fmt.Errorf("未知操作: %q", req.Op)
	}
}

// rotationArgs 是协议里 league.rotation 的参数。
type rotationArgs struct {
	Players     []calc.Player `json:"players"`
	TeamSize    int           `json:"teamSize"`
	TotalRounds int           `json:"totalRounds"`
	Seed        *int64        `json:"seed"`
	Fair        bool          `json:"fair"`
}

func leagueRotation(raw json.RawMessage) (any, error) {
	var args rotationArgs
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&args); err != nil {
		return nil, fmt.Errorf("参数解析失败: %w", err)
	}
	result, err := calc.GenerateRotation(args.Players, args.TeamSize, args.TotalRounds, args.Seed, args.Fair)
	if err != nil {
		return nil, err
	}
	log.Printf(
		"轮换赛程完成 | 选手=%d | 每队=%d | 局数=%d | 起点=%d | 代价=%.4f",
		len(args.Players), args.TeamSize, args.TotalRounds, result.Restarts, result.Cost,
	)
	return result, nil
}

// selftest 用最小规模跑一遍，供部署后确认二进制能正常工作。
func selftest() error {
	players := []calc.Player{
		{ID: "p1", Name: "a"}, {ID: "p2", Name: "b"},
		{ID: "p3", Name: "c"}, {ID: "p4", Name: "d"},
		{ID: "p5", Name: "e"}, {ID: "p6", Name: "f"}, {ID: "p7", Name: "g"},
	}
	res, err := calc.GenerateRotation(players, 2, 3, nil, true)
	if err != nil {
		return err
	}
	if len(res.Rounds) != 3 {
		return fmt.Errorf("局数不对: %d", len(res.Rounds))
	}
	return nil
}
