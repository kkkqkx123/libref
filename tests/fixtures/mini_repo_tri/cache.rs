pub struct Cache {
    count: u32,
}

impl Cache {
    pub fn new() -> Cache {
        Cache { count: 0 }
    }

    pub fn get(&self) -> u32 {
        self.count
    }
}
