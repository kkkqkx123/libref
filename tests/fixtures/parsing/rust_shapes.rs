use crate::store::{load, save as store_save};

/// Cache holds fetched items.
pub struct Cache {
    items: u32,
}

impl Cache {
    pub fn new() -> Cache {
        Cache { items: 0 }
    }

    /// Return the stored count.
    pub fn get(&self) -> u32 {
        self.helper();
        self.items
    }
}

pub fn run(path: String) -> u32 {
    let cache = Cache::new();
    let value = cache.get();
    load(path);
    store_save(value);
    value
}
