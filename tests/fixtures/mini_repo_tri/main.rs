use crate::cache::Cache;

fn main() {
    let cache = Cache::new();
    let _ = cache.get();
}
