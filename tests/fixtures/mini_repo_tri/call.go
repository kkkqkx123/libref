package call

import "example.com/demo/util"

// Run fetches twice.
func Run(v int) int {
	first := util.Fetch(v)
	return util.Fetch(first)
}
