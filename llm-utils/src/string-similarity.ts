/**
 * Calculates the similarity ratio between two strings (0 to 1)
 *
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns A value between 0 (completely different) and 1 (identical)
 */
export function stringSimilarity(str1: string, str2: string): number {
    const maxLength = Math.max(str1.length, str2.length);
    if (maxLength === 0) return 1.0; // Both strings are empty
    const distance = levenshteinDistance(str1, str2);
    return 1 - distance / maxLength;
}

/**
 * Calculates the Levenshtein distance between two strings
 *
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns The Levenshtein distance (number of edits needed to transform str1 into str2)
 */
export function levenshteinDistance(str1: string, str2: string): number {
    const m = str1.length;
    const n = str2.length;
    const dp: number[][] = Array(m + 1).fill(null).map(() => Array(n + 1).fill(0));

    for (let i = 0; i <= m; i++) dp[i][0] = i;
    for (let j = 0; j <= n; j++) dp[0][j] = j;

    for (let i = 1; i <= m; i++) {
        for (let j = 1; j <= n; j++) {
            if (str1[i - 1] === str2[j - 1]) {
                dp[i][j] = dp[i - 1][j - 1];
            } else {
                dp[i][j] = Math.min(
                    dp[i - 1][j - 1] + 1,  // substitution
                    dp[i - 1][j] + 1,      // deletion
                    dp[i][j - 1] + 1       // insertion
                );
            }
        }
    }
    return dp[m][n];
}
