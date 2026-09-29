/// Clamp a percentage to the 0..=100 range.
pub fn clamp_percent(value: i32) -> i32 {
    value.clamp(0, 100)
}

#[cfg(test)]
mod tests {
    use super::clamp_percent;

    #[test]
    fn clamps_both_ends() {
        assert_eq!(clamp_percent(-5), 0);
        assert_eq!(clamp_percent(250), 100);
    }
}
