package store

import (
	"fmt"
	alias "example.com/x/y"
)

// Cache holds fetched items.
type Cache struct {
	items int
}

// Get returns the stored count.
func (c *Cache) Get() int {
	return c.items
}

// Load fetches and prints.
func Load(path string) int {
	cache := Cache{}
	value := cache.Get()
	fmt.Println(path)
	return alias.Fetch(value)
}
